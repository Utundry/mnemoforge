"""
Cloud LLM client — OpenAI-compatible interface.

Supports configurable external providers such as Gemini and GLM while keeping
legacy `GLM_*` settings as a backward-compatible fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

_GENERIC_PROVIDER_LABEL = "openai-compatible"


@dataclass(frozen=True)
class CloudLLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str


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


def _current_cloud_config() -> CloudLLMConfig | None:
    if settings.cloud_llm_api_key:
        return CloudLLMConfig(
            provider=_infer_provider_name(
                provider=settings.cloud_llm_provider,
                model=settings.cloud_llm_model,
                base_url=settings.cloud_llm_base_url,
            ),
            api_key=settings.cloud_llm_api_key,
            model=settings.cloud_llm_model,
            base_url=settings.cloud_llm_base_url,
        )

    if settings.glm_api_key:
        return CloudLLMConfig(
            provider="glm",
            api_key=settings.glm_api_key,
            model=settings.glm_model,
            base_url=settings.glm_base_url,
        )

    return None


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


def cloud_available() -> bool:
    """Return True if a cloud LLM is configured and ready to use."""
    return _current_cloud_config() is not None


def cloud_provider() -> str:
    """Return active cloud provider name for tracker/registry."""
    config = _current_cloud_config()
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
) -> str:
    """
    Call configured cloud LLM via OpenAI-compatible chat completions.

    Returns text response.
    Raises RuntimeError if no cloud LLM configured.
    Raises httpx.HTTPError on network/API errors.
    """
    config = _current_cloud_config()
    if not config:
        raise RuntimeError(
            "No cloud LLM configured. Set CLOUD_LLM_* (recommended) or legacy GLM_* in .env"
        )

    url = f"{config.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
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
