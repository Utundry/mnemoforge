from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from app.config import settings
from app.services.capability_registry import get_registry
from app.services.lmstudio_service import LMStudioService
from app.services.ollama_service import OllamaService

logger = logging.getLogger(__name__)

_LOCAL_THRESHOLD = 0.65
_LOCAL_FRIENDLY_TASK_TYPES = {
    "text_summarization",
    "fact_extraction",
    "query_expansion",
    "skill_tagging",
    "memory_extraction",
}


def _parse_model_list(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _dedupe_keep_order(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = str(item).strip()
        if not key or key in seen:
            continue
        result.append(key)
        seen.add(key)
    return result


def _estimate_token_units(*parts: str) -> int:
    chars = sum(len(part or "") for part in parts)
    return max(1, chars // 4)


class CloudLLMGateway:
    """
    Budget-aware cloud/local LLM gateway.

    Selection order is configurable through env lists so the project can route:
    - cheap local SLM for low-risk synthesis
    - economy cloud models for routine summaries/docs
    - balanced/reasoning cloud models for harder tasks
    """

    def __init__(self) -> None:
        configured_model = settings.cloud_llm_model or settings.glm_model or "glm-4.5-air"
        self.primary_model = (settings.primary_cloud_llm or configured_model).strip() or configured_model
        self.fallback_models = _parse_model_list(settings.fallback_cloud_llms)
        self.economy_models = _dedupe_keep_order(
            _parse_model_list(settings.economy_cloud_llms) + [configured_model] + self.fallback_models
        )
        self.balanced_models = _dedupe_keep_order(
            _parse_model_list(settings.balanced_cloud_llms) + [configured_model, self.primary_model] + self.fallback_models
        )
        self.reasoning_models = _dedupe_keep_order(
            _parse_model_list(settings.reasoning_cloud_llms) + [self.primary_model, configured_model] + self.fallback_models
        )
        self.local_model = os.getenv("LOCAL_GENERATE_MODEL", settings.learning_mirror_model or "qwen3:1.7b").strip() or "qwen3:1.7b"
        self.local_provider = os.getenv("LOCAL_LLM_PROVIDER", settings.local_llm_provider).strip().lower() or "auto"
        self.lmstudio_model = os.getenv("LMSTUDIO_MODEL", settings.lmstudio_model).strip() or settings.lmstudio_model
        self.enable_local_fallback = os.getenv("LLM_GATEWAY_ENABLE_LOCAL_FALLBACK", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._local_service: OllamaService | None = None
        self._lmstudio_service: LMStudioService | None = None

    def _provider_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        if settings.gemini_model:
            aliases["gemini"] = settings.gemini_model.strip()
        if settings.glm_model:
            aliases["glm"] = settings.glm_model.strip()
        if settings.deepseek_model:
            aliases["deepseek"] = settings.deepseek_model.strip()
        if settings.cloud_llm_model:
            aliases["cloud"] = settings.cloud_llm_model.strip()
        aliases["primary"] = self.primary_model
        return {key: value for key, value in aliases.items() if value}

    def _resolve_model_aliases(self, models: list[str]) -> list[str]:
        aliases = self._provider_aliases()
        resolved: list[str] = []
        for model in models:
            key = str(model or "").strip()
            if not key:
                continue
            resolved.append(aliases.get(key.lower(), key))
        return _dedupe_keep_order(resolved)

    def _cloud_module(self):
        from app.services import cloud_llm as _cloud_llm

        return _cloud_llm

    def _registry_ranked_models(self, task_type: str | None) -> list[str]:
        if not task_type:
            return []
        try:
            from app.services.model_registry import get_model_registry

            return [model_id for model_id, _score in get_model_registry().rank_for_task(task_type)]
        except Exception:
            return []

    def _profiled_models(self) -> list[str]:
        try:
            return list(self._cloud_module().available_cloud_models())
        except Exception:
            return []

    def _callable_cloud_models(self, mode_models: list[str]) -> list[str]:
        # Only retry models we can actually reach with configured credentials:
        # explicit mode/env hints plus profiled cross-provider configs.
        return _dedupe_keep_order(self._resolve_model_aliases(mode_models) + self._profiled_models())

    def _candidate_models(self, mode: str, *, task_type: str | None = None, model_override: str | None = None) -> list[str]:
        if model_override:
            return [model_override]
        normalized = (mode or "balanced").strip().lower()
        if normalized in {"economy", "strict_economy"}:
            mode_models = self.economy_models or [self.primary_model]
        elif normalized in {"max_quality", "reasoning"}:
            mode_models = self.reasoning_models or [self.primary_model]
        else:
            mode_models = self.balanced_models or [self.primary_model]
        callable_models = self._callable_cloud_models(mode_models)
        ranked_models = [model_id for model_id in self._registry_ranked_models(task_type) if model_id in callable_models]
        return _dedupe_keep_order(self._resolve_model_aliases(mode_models) + self._profiled_models() + ranked_models)

    def _known_model_available(self, model_id: str) -> bool:
        try:
            from app.services.model_registry import get_model_registry

            return bool(get_model_registry().get_model(model_id).is_available)
        except Exception:
            return True

    def _filter_models(self, models: list[str]) -> list[str]:
        filtered = [model for model in models if self._known_model_available(model)]
        return filtered or models

    def _local_score(self, task_type: str | None) -> float:
        if not task_type:
            return 0.0
        registry = get_registry()
        score = registry.score(self.local_model, task_type)
        if score <= 0 and self.local_model != "qwen3:1.7b":
            score = registry.score("qwen3:1.7b", task_type)
        return score

    def _should_try_local(self, *, task_type: str | None, mode: str, prefer_local: bool) -> bool:
        if not self.enable_local_fallback or not task_type:
            return False
        score = self._local_score(task_type)
        if prefer_local and score >= _LOCAL_THRESHOLD:
            return True
        if mode in {"economy", "strict_economy"} and (
            score >= _LOCAL_THRESHOLD or task_type in _LOCAL_FRIENDLY_TASK_TYPES
        ):
            return True
        return False

    async def _generate_local(
        self,
        *,
        prompt: str,
        timeout: float,
    ) -> str:
        errors: list[str] = []
        for provider in self._local_provider_order():
            if provider == "ollama":
                if self._local_service is None:
                    self._local_service = OllamaService()
                result = (await self._local_service.generate(prompt, model=self.local_model, timeout=min(timeout, 45.0))).strip()
                if result:
                    return result
                errors.append("ollama")
            elif provider == "lmstudio":
                if self._lmstudio_service is None:
                    self._lmstudio_service = LMStudioService()
                result = (
                    await self._lmstudio_service.generate(
                        prompt,
                        model=self.lmstudio_model,
                        timeout=min(timeout, 45.0),
                    )
                ).strip()
                if result:
                    return result
                errors.append("lmstudio")
        logger.debug("All local LLM providers returned empty output: %s", ", ".join(errors))
        return ""

    def _local_provider_order(self) -> list[str]:
        configured = self.local_provider
        if configured in {"ollama", "lmstudio"}:
            return [configured]
        raw = os.getenv("LOCAL_LLM_FALLBACK_ORDER", settings.local_llm_fallback_order)
        providers = [item.strip().lower() for item in (raw or "").split(",") if item.strip()]
        ordered: list[str] = []
        for provider in providers or ["ollama", "lmstudio"]:
            if provider in {"ollama", "lmstudio"} and provider not in ordered:
                ordered.append(provider)
        return ordered or ["ollama", "lmstudio"]

    def _record_cloud_usage(self, *, model_id: str, prompt: str, system: str, response: str) -> None:
        try:
            from app.services.model_registry import get_model_registry

            get_model_registry().record_usage(
                model_id,
                _estimate_token_units(prompt, system, response),
            )
        except Exception:
            pass

    def _report_limit_hit(self, *, model_id: str, exc: Exception) -> None:
        if not isinstance(exc, httpx.HTTPStatusError):
            return
        status = int(exc.response.status_code or 0)
        if status not in {429, 500, 502, 503, 504}:
            return
        retry_after = None
        try:
            header = exc.response.headers.get("Retry-After")
            if header:
                retry_after = int(float(header))
        except Exception:
            retry_after = None
        try:
            from app.services.model_registry import get_model_registry

            get_model_registry().report_limit_hit(
                model_id,
                error_code=str(status),
                error_msg=str(exc),
                retry_after=retry_after or 900,
            )
        except Exception:
            pass

    async def generate(
        self,
        prompt: str,
        system: str = "You are a helpful assistant.",
        require_consensus: bool = False,
        *,
        task_type: str | None = None,
        mode: str = "balanced",
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout: float = 60.0,
        model_override: str | None = None,
        allow_local_fallback: bool = False,
        prefer_local: bool = False,
    ) -> str:
        """Generate text using the cheapest suitable configured tier first."""
        if require_consensus:
            mode = "max_quality"

        if allow_local_fallback and self._should_try_local(task_type=task_type, mode=mode, prefer_local=prefer_local):
            try:
                local = await self._generate_local(prompt=prompt, timeout=timeout)
                if local:
                    return local
            except Exception as exc:
                logger.debug("Local gateway fallback failed for %s: %s", task_type or "generic", exc)

        cloud_mod = self._cloud_module()
        if not cloud_mod.cloud_available():
            if allow_local_fallback:
                local = await self._generate_local(prompt=prompt, timeout=timeout)
                if local:
                    return local
            raise RuntimeError("CloudLLMGateway: no cloud LLM configured and local fallback unavailable")

        models_to_try = self._filter_models(
            self._candidate_models(mode, task_type=task_type, model_override=model_override)
        )
        last_error: Exception | None = None
        for model in models_to_try:
            try:
                logger.debug("LLM gateway trying model=%s mode=%s task_type=%s", model, mode, task_type or "generic")
                response = await cloud_mod.cloud_complete(
                    prompt=prompt,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout,
                    model_override=model,
                )
                self._record_cloud_usage(model_id=model, prompt=prompt, system=system, response=response)
                return response
            except Exception as exc:
                last_error = exc
                self._report_limit_hit(model_id=model, exc=exc)
                logger.warning("Gateway model %s failed: %s", model, cloud_mod.describe_cloud_error(exc))

        if allow_local_fallback:
            local = await self._generate_local(prompt=prompt, timeout=timeout)
            if local:
                return local

        raise RuntimeError(f"CloudLLMGateway: all configured models failed: {last_error}")


def get_cloud_gateway() -> CloudLLMGateway:
    """Factory helper for service-level budgeted calls."""
    return CloudLLMGateway()
