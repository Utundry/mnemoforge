"""
Cloud LLM client with pluggable provider API styles.

Supports configurable external providers such as Gemini and GLM while keeping
legacy `GLM_*` settings as a backward-compatible fallback.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

_GENERIC_PROVIDER_LABEL = "openai-compatible"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloudLLMConfig:
    provider: str
    api_style: str
    auth_style: str
    api_key: str
    model: str
    base_url: str


def _normalize_api_style(api_style: str) -> str:
    normalized = (api_style or "").strip().lower().replace("_", "-")
    aliases = {
        "openai": "openai-chat",
        "openai-compatible": "openai-chat",
        "chat-completions": "openai-chat",
        "gemini": "gemini-native",
        "generatecontent": "gemini-native",
        "gemini-generatecontent": "gemini-native",
    }
    return aliases.get(normalized, normalized)


def _normalize_auth_style(auth_style: str) -> str:
    normalized = (auth_style or "").strip().lower().replace("_", "-")
    aliases = {
        "bearer-token": "bearer",
        "plain": "raw",
        "direct": "raw",
        "x-goog-api-key": "x-goog-api-key",
    }
    return aliases.get(normalized, normalized)


def _normalize_provider_name(provider: str) -> str:
    normalized = (provider or "").strip().lower().replace("_", "-")
    aliases = {
        "google": "gemini",
        "google-ai": "gemini",
        "google-gemini": "gemini",
        "zhipu": "glm",
        "z-ai": "glm",
        "bigmodel": "glm",
    }
    return aliases.get(normalized, normalized)


def _disabled_cloud_llms() -> set[str]:
    disabled = {
        item.strip().lower()
        for item in (settings.disabled_cloud_llms or "").split(",")
        if item.strip()
    }
    disabled.update(_disabled_profile_targets())
    return disabled


def _is_enabled_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if not normalized:
        return True
    return normalized not in {"0", "false", "no", "off", "disabled", "inactive"}


def _profile_enabled(entry: dict[str, Any]) -> bool:
    if "enabled" in entry:
        return _is_enabled_flag(entry.get("enabled"))
    if "active" in entry:
        return _is_enabled_flag(entry.get("active"))
    return True


def _read_configured_secret(name: str) -> str:
    key = (name or "").strip()
    if not key:
        return ""
    value = os.getenv(key, "").strip()
    if value:
        return value
    settings_attr = key.lower()
    return str(getattr(settings, settings_attr, "") or "").strip()


def _disabled_profile_targets() -> set[str]:
    raw = (os.getenv("CLOUD_LLM_MODEL_PROFILES") or settings.cloud_llm_model_profiles or "").strip()
    if not raw:
        return set()

    try:
        payload = json.loads(raw)
    except Exception:
        return set()

    if isinstance(payload, list):
        entries = [entry for entry in payload if isinstance(entry, dict)]
    elif isinstance(payload, dict):
        entries = []
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            entry = dict(value)
            entry.setdefault("model_id", key)
            entries.append(entry)
    else:
        return set()

    disabled: set[str] = set()
    for entry in entries:
        if _profile_enabled(entry):
            continue
        model_id = str(entry.get("model_id") or entry.get("model") or "").strip().lower()
        provider = _normalize_provider_name(str(entry.get("provider") or "")).strip().lower()
        if model_id:
            disabled.add(model_id)
        if provider:
            disabled.add(provider)
    return disabled


def _is_cloud_llm_enabled(*, model_id: str, provider: str) -> bool:
    disabled = _disabled_cloud_llms()
    if not disabled:
        return True
    return model_id.strip().lower() not in disabled and _normalize_provider_name(provider) not in disabled


def _infer_provider_name(*, provider: str, model: str, base_url: str) -> str:
    explicit = _normalize_provider_name(provider)
    if explicit:
        return explicit

    haystack = f"{model} {base_url}".lower()
    if "gemini" in haystack or "generativelanguage.googleapis.com" in haystack:
        return "gemini"
    if "glm" in haystack or "bigmodel.cn" in haystack or "z.ai" in haystack:
        return "glm"
    return _GENERIC_PROVIDER_LABEL


def _infer_api_style(*, provider: str, model: str, base_url: str, api_style: str = "") -> str:
    explicit = _normalize_api_style(api_style)
    if explicit:
        return explicit

    normalized_provider = _infer_provider_name(provider=provider, model=model, base_url=base_url)
    base = (base_url or "").lower()
    if normalized_provider == "gemini" and "/openai" not in base:
        return "gemini-native"
    return "openai-chat"


def _infer_auth_style(*, provider: str, base_url: str, api_style: str, auth_style: str = "") -> str:
    explicit = _normalize_auth_style(auth_style)
    if explicit:
        return explicit

    if api_style == "gemini-native":
        return "x-goog-api-key"

    normalized_provider = _normalize_provider_name(provider)
    base = (base_url or "").lower()
    if normalized_provider == "glm" and "api.z.ai" in base:
        return "raw"
    return "bearer"


def _current_cloud_config() -> CloudLLMConfig | None:
    if (
        settings.cloud_llm_api_key
        and settings.cloud_llm_model
        and _is_cloud_llm_enabled(model_id=settings.cloud_llm_model, provider=settings.cloud_llm_provider)
    ):
        return CloudLLMConfig(
            provider=_infer_provider_name(
                provider=settings.cloud_llm_provider,
                model=settings.cloud_llm_model,
                base_url=settings.cloud_llm_base_url,
            ),
            api_style=_infer_api_style(
                provider=settings.cloud_llm_provider,
                model=settings.cloud_llm_model,
                base_url=settings.cloud_llm_base_url,
            ),
            auth_style=_infer_auth_style(
                provider=settings.cloud_llm_provider,
                base_url=settings.cloud_llm_base_url,
                api_style=_infer_api_style(
                    provider=settings.cloud_llm_provider,
                    model=settings.cloud_llm_model,
                    base_url=settings.cloud_llm_base_url,
                ),
            ),
            api_key=settings.cloud_llm_api_key,
            model=settings.cloud_llm_model,
            base_url=settings.cloud_llm_base_url,
        )

    if settings.glm_api_key and settings.glm_model and _is_cloud_llm_enabled(model_id=settings.glm_model, provider="glm"):
        return CloudLLMConfig(
            provider="glm",
            api_style="openai-chat",
            auth_style=_infer_auth_style(
                provider="glm",
                base_url=settings.glm_base_url,
                api_style="openai-chat",
            ),
            api_key=settings.glm_api_key,
            model=settings.glm_model,
            base_url=settings.glm_base_url,
        )

    if (
        settings.deepseek_api_key
        and settings.deepseek_model
        and _is_cloud_llm_enabled(model_id=settings.deepseek_model, provider="deepseek")
    ):
        return CloudLLMConfig(
            provider="deepseek",
            api_style="openai-chat",
            auth_style="bearer",
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
        )

    return None


def _load_profiled_configs() -> dict[str, CloudLLMConfig]:
    configs: dict[str, CloudLLMConfig] = {}

    if (
        settings.cloud_llm_api_key
        and settings.cloud_llm_model
        and _is_cloud_llm_enabled(model_id=settings.cloud_llm_model, provider=settings.cloud_llm_provider)
    ):
        configs[settings.cloud_llm_model] = CloudLLMConfig(
            provider=_infer_provider_name(
                provider=settings.cloud_llm_provider,
                model=settings.cloud_llm_model,
                base_url=settings.cloud_llm_base_url,
            ),
            api_style=_infer_api_style(
                provider=settings.cloud_llm_provider,
                model=settings.cloud_llm_model,
                base_url=settings.cloud_llm_base_url,
            ),
            auth_style=_infer_auth_style(
                provider=settings.cloud_llm_provider,
                base_url=settings.cloud_llm_base_url,
                api_style=_infer_api_style(
                    provider=settings.cloud_llm_provider,
                    model=settings.cloud_llm_model,
                    base_url=settings.cloud_llm_base_url,
                ),
            ),
            api_key=settings.cloud_llm_api_key,
            model=settings.cloud_llm_model,
            base_url=settings.cloud_llm_base_url,
        )

    if settings.glm_api_key and settings.glm_model and _is_cloud_llm_enabled(model_id=settings.glm_model, provider="glm"):
        configs[settings.glm_model] = CloudLLMConfig(
            provider="glm",
            api_style="openai-chat",
            auth_style=_infer_auth_style(
                provider="glm",
                base_url=settings.glm_base_url,
                api_style="openai-chat",
            ),
            api_key=settings.glm_api_key,
            model=settings.glm_model,
            base_url=settings.glm_base_url,
        )

    if (
        settings.deepseek_api_key
        and settings.deepseek_model
        and _is_cloud_llm_enabled(model_id=settings.deepseek_model, provider="deepseek")
    ):
        configs[settings.deepseek_model] = CloudLLMConfig(
            provider="deepseek",
            api_style="openai-chat",
            auth_style="bearer",
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
        )

    if (
        settings.gemini_api_key
        and settings.gemini_model
        and _is_cloud_llm_enabled(model_id=settings.gemini_model, provider="gemini")
    ):
        configs[settings.gemini_model] = CloudLLMConfig(
            provider="gemini",
            api_style="gemini-native",
            auth_style="x-goog-api-key",
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            base_url=settings.gemini_base_url,
        )

    raw = (os.getenv("CLOUD_LLM_MODEL_PROFILES") or settings.cloud_llm_model_profiles or "").strip()
    if not raw:
        return configs

    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to parse CLOUD_LLM_MODEL_PROFILES: %s", exc)
        return configs

    if isinstance(payload, list):
        items = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("model_id") or entry.get("model") or "").strip()
            if model_id:
                items.append((model_id, entry))
    elif isinstance(payload, dict):
        items = []
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            fallback_key = str(key).strip()
            model_id = str(value.get("model_id") or value.get("model") or fallback_key).strip()
            if model_id:
                items.append((model_id, value))
    else:
        logger.warning("CLOUD_LLM_MODEL_PROFILES must be a JSON object or list")
        return configs

    for model_id, entry in items:
        provider = str(entry.get("provider") or "").strip()
        api_style = str(entry.get("api_style") or "").strip()
        auth_style = str(entry.get("auth_style") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        api_key = str(entry.get("api_key") or "").strip()
        api_key_env = str(entry.get("api_key_env") or "").strip()
        if not api_key and api_key_env:
            api_key = _read_configured_secret(api_key_env)
        model = str(entry.get("model") or model_id).strip() or model_id
        if not _profile_enabled(entry):
            configs.pop(model_id, None)
            continue
        if not api_key or not base_url or not model_id:
            logger.warning("Skipping incomplete cloud model profile for %s", model_id or "<empty>")
            continue
        if not _is_cloud_llm_enabled(model_id=model_id, provider=provider):
            continue
        configs[model_id] = CloudLLMConfig(
            provider=_infer_provider_name(provider=provider, model=model, base_url=base_url),
            api_style=_infer_api_style(provider=provider, model=model, base_url=base_url, api_style=api_style),
            auth_style=_infer_auth_style(
                provider=provider,
                base_url=base_url,
                api_style=_infer_api_style(provider=provider, model=model, base_url=base_url, api_style=api_style),
                auth_style=auth_style,
            ),
            api_key=api_key,
            model=model,
            base_url=base_url,
        )

    return configs


def available_cloud_models() -> list[str]:
    """Return cloud model ids with dedicated configured credentials/endpoints."""
    return list(_load_profiled_configs().keys())


def configured_cloud_model_profiles() -> dict[str, CloudLLMConfig]:
    """Return configured cloud model profiles keyed by model id."""
    return _load_profiled_configs()


def has_cloud_profile(model_id: str) -> bool:
    return str(model_id or "").strip() in _load_profiled_configs()


def _resolve_cloud_config(model_override: str | None = None) -> CloudLLMConfig | None:
    override = str(model_override or "").strip()
    profiled = _load_profiled_configs()
    if override and override in profiled:
        return profiled[override]

    base = _current_cloud_config()
    if not base:
        if override:
            return profiled.get(override)
        if profiled:
            return next(iter(profiled.values()))
        return None
    if not override:
        return base

    return CloudLLMConfig(
        provider=_infer_provider_name(provider=base.provider, model=override, base_url=base.base_url),
        api_style=base.api_style,
        auth_style=base.auth_style,
        api_key=base.api_key,
        model=override,
        base_url=base.base_url,
    )


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                chunks.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
        return "\n".join(chunks).strip()

    return ""


def _extract_gemini_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return ""

    chunks: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


def cloud_available() -> bool:
    """Return True if a cloud LLM is configured and ready to use."""
    return _resolve_cloud_config() is not None


def cloud_provider(model_override: str | None = None) -> str:
    """Return active cloud provider name for tracker/registry."""
    config = _resolve_cloud_config(model_override=model_override)
    if not config:
        return "cloud-llm"
    return f"{config.provider}:{config.model}"


def describe_cloud_error(exc: Exception) -> str:
    """Return a readable cloud/API error message for logs and UI details."""
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        body = ""
        try:
            body = (response.text or "").strip()
        except Exception:
            body = ""
        if body:
            body = " ".join(body.split())
            if len(body) > 300:
                body = body[:297] + "..."
            return f"HTTP {response.status_code}: {body}"
        return f"HTTP {response.status_code}"

    if isinstance(exc, httpx.TimeoutException):
        return "Cloud LLM request timed out"

    if isinstance(exc, httpx.RequestError):
        base = str(exc).strip()
        return base or exc.__class__.__name__

    base = str(exc).strip()
    return base or exc.__class__.__name__


async def cloud_complete(
    prompt: str,
    *,
    system: str = "You are a helpful assistant.",
    max_tokens: int = 2048,
    temperature: float = 0.3,
    timeout: float = 60.0,
    model_override: str | None = None,
) -> str:
    """
    Call configured cloud LLM using the profile's configured API style.

    Returns text response.
    Raises RuntimeError if no cloud LLM configured.
    Raises httpx.HTTPError on network/API errors.
    """
    config = _resolve_cloud_config(model_override=model_override)
    if not config:
        raise RuntimeError(
            "No cloud LLM configured. Set CLOUD_LLM_* (recommended) or legacy GLM_* in .env"
        )

    if config.api_style == "gemini-native":
        url = f"{config.base_url.rstrip('/')}/models/{config.model}:generateContent"
        headers = {
            "x-goog-api-key": config.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": f"{system.strip()}\n\n{prompt.strip()}".strip(),
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
    else:
        url = f"{config.base_url.rstrip('/')}/chat/completions"
        auth_value = config.api_key if config.auth_style == "raw" else f"Bearer {config.api_key}"
        headers = {
            "Authorization": auth_value,
            "Content-Type": "application/json",
        }
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    if config.api_style == "gemini-native":
        content = _extract_gemini_text(data)
        if not content:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}")
        return content

    try:
        msg = data["choices"][0]["message"]
        content = _extract_text(msg.get("content"))
        if not content:
            content = _extract_text(msg.get("reasoning_content"))
        if not content:
            raise RuntimeError(f"Empty response from cloud LLM: {data}")
        return content
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected cloud LLM response shape: {exc} — {data}") from exc
