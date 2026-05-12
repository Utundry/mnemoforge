from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.services.llm_gateway import CloudLLMGateway


@pytest.fixture
def reset_cloud_settings():
    snapshot = {
        "cloud_llm_provider": settings.cloud_llm_provider,
        "cloud_llm_api_key": settings.cloud_llm_api_key,
        "cloud_llm_model": settings.cloud_llm_model,
        "cloud_llm_base_url": settings.cloud_llm_base_url,
        "glm_api_key": settings.glm_api_key,
        "glm_model": settings.glm_model,
        "glm_base_url": settings.glm_base_url,
        "gemini_api_key": settings.gemini_api_key,
        "gemini_model": settings.gemini_model,
        "gemini_base_url": settings.gemini_base_url,
        "deepseek_api_key": settings.deepseek_api_key,
        "deepseek_model": settings.deepseek_model,
        "deepseek_base_url": settings.deepseek_base_url,
        "local_llm_provider": settings.local_llm_provider,
        "local_llm_fallback_order": settings.local_llm_fallback_order,
        "lmstudio_base_url": settings.lmstudio_base_url,
        "lmstudio_model": settings.lmstudio_model,
        "primary_cloud_llm": settings.primary_cloud_llm,
        "fallback_cloud_llms": settings.fallback_cloud_llms,
        "economy_cloud_llms": settings.economy_cloud_llms,
        "balanced_cloud_llms": settings.balanced_cloud_llms,
        "reasoning_cloud_llms": settings.reasoning_cloud_llms,
        "cloud_llm_model_profiles": settings.cloud_llm_model_profiles,
        "disabled_cloud_llms": settings.disabled_cloud_llms,
    }
    settings.cloud_llm_model_profiles = ""
    settings.disabled_cloud_llms = ""
    settings.local_llm_provider = "auto"
    settings.local_llm_fallback_order = "ollama,lmstudio"
    settings.lmstudio_model = "auto"
    yield
    for key, value in snapshot.items():
        setattr(settings, key, value)


@pytest.mark.asyncio
async def test_gateway_prefers_local_for_economy_summarization(monkeypatch, reset_cloud_settings):
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    monkeypatch.setattr(
        "app.services.cloud_llm.cloud_complete",
        AsyncMock(side_effect=AssertionError("cloud path should not be used")),
    )

    gateway = CloudLLMGateway()
    monkeypatch.setattr(gateway, "_generate_local", AsyncMock(return_value="local summary"))

    result = await gateway.generate(
        "Summarize the project status.",
        task_type="text_summarization",
        mode="economy",
        allow_local_fallback=True,
        prefer_local=True,
    )

    assert result == "local summary"


@pytest.mark.asyncio
async def test_gateway_uses_economy_cloud_chain_with_fallback(monkeypatch, reset_cloud_settings):
    settings.cloud_llm_provider = "openai-compatible"
    settings.cloud_llm_api_key = "generic-key"
    settings.cloud_llm_model = "cheap-model"
    settings.cloud_llm_base_url = "https://example.invalid/v1"
    settings.glm_api_key = ""
    settings.gemini_api_key = ""
    settings.deepseek_api_key = ""
    settings.economy_cloud_llms = "cheap-model,backup-model"
    settings.balanced_cloud_llms = ""
    settings.reasoning_cloud_llms = ""
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    calls: list[str] = []

    async def fake_cloud_complete(prompt: str, **kwargs):
        model = kwargs.get("model_override") or ""
        calls.append(str(model))
        if model == "cheap-model":
            request = httpx.Request("POST", "https://example.invalid/chat/completions")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return "backup ok"

    monkeypatch.setattr("app.services.cloud_llm.cloud_complete", fake_cloud_complete)

    gateway = CloudLLMGateway()
    monkeypatch.setattr(gateway, "_known_model_available", lambda model_id: True)
    result = await gateway.generate(
        "Write a short architecture note.",
        task_type="architecture",
        mode="economy",
        allow_local_fallback=False,
    )

    assert result == "backup ok"
    assert calls[:2] == ["cheap-model", "backup-model"]


@pytest.mark.asyncio
async def test_gateway_retries_on_context_limit_with_profiled_alternate(monkeypatch, reset_cloud_settings):
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_provider = ""
    settings.cloud_llm_model = ""
    settings.cloud_llm_base_url = ""
    settings.glm_api_key = "glm-key"
    settings.glm_model = "glm-4.5-air"
    settings.glm_base_url = "https://api.z.ai/api/coding/paas/v4"
    settings.gemini_api_key = "gem-key"
    settings.gemini_model = "gemini-3-flash-preview"
    settings.gemini_base_url = "https://generativelanguage.googleapis.com/v1beta"
    settings.economy_cloud_llms = ""
    settings.balanced_cloud_llms = "gemini,glm"
    settings.reasoning_cloud_llms = ""

    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    calls: list[str] = []

    async def fake_cloud_complete(prompt: str, **kwargs):
        model = kwargs.get("model_override") or ""
        calls.append(str(model))
        if model == "gemini-3-flash-preview":
            request = httpx.Request("POST", "https://example.invalid/chat/completions")
            response = httpx.Response(400, request=request, text="context length exceeded")
            raise httpx.HTTPStatusError("context overflow", request=request, response=response)
        return "glm finished"

    monkeypatch.setattr("app.services.cloud_llm.cloud_complete", fake_cloud_complete)

    gateway = CloudLLMGateway()
    monkeypatch.setattr(gateway, "_known_model_available", lambda model_id: True)
    result = await gateway.generate(
        "Continue despite context limit.",
        task_type="architecture",
        mode="balanced",
        allow_local_fallback=False,
    )

    assert result == "glm finished"
    assert calls[:2] == ["gemini-3-flash-preview", "glm-4.5-air"]


@pytest.mark.asyncio
async def test_gateway_resolves_provider_alias_lists(monkeypatch, reset_cloud_settings):
    settings.economy_cloud_llms = "gemini,glm"
    settings.balanced_cloud_llms = ""
    settings.reasoning_cloud_llms = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_provider = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = "glm-key"
    settings.glm_model = "glm-4.7"
    settings.gemini_api_key = "gem-key"
    settings.gemini_model = "gemini-3-flash-preview"
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    calls: list[str] = []

    async def fake_cloud_complete(prompt: str, **kwargs):
        model = kwargs.get("model_override") or ""
        calls.append(str(model))
        if model == "gemini-3-flash-preview":
            request = httpx.Request("POST", "https://example.invalid")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return "glm ok"

    monkeypatch.setattr("app.services.cloud_llm.cloud_complete", fake_cloud_complete)

    gateway = CloudLLMGateway()
    monkeypatch.setattr(gateway, "_known_model_available", lambda model_id: True)
    result = await gateway.generate(
        "Write a short architecture note.",
        task_type="architecture",
        mode="economy",
        allow_local_fallback=False,
    )

    assert result == "glm ok"
    assert calls[:2] == ["gemini-3-flash-preview", "glm-4.7"]


@pytest.mark.asyncio
async def test_gateway_resolves_deepseek_provider_alias(monkeypatch, reset_cloud_settings):
    settings.economy_cloud_llms = "gemini,deepseek"
    settings.balanced_cloud_llms = ""
    settings.reasoning_cloud_llms = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_provider = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = ""
    settings.gemini_api_key = "gem-key"
    settings.gemini_model = "gemini-3.1-flash"
    settings.deepseek_api_key = "deepseek-key"
    settings.deepseek_model = "deepseek-chat"
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    calls: list[str] = []

    async def fake_cloud_complete(prompt: str, **kwargs):
        model = kwargs.get("model_override") or ""
        calls.append(str(model))
        if model == "gemini-3.1-flash":
            request = httpx.Request("POST", "https://example.invalid")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)
        return "deepseek ok"

    monkeypatch.setattr("app.services.cloud_llm.cloud_complete", fake_cloud_complete)

    gateway = CloudLLMGateway()
    monkeypatch.setattr(gateway, "_known_model_available", lambda model_id: True)
    result = await gateway.generate(
        "Write a short architecture note.",
        task_type="architecture",
        mode="economy",
        allow_local_fallback=False,
    )

    assert result == "deepseek ok"
    assert calls[:2] == ["gemini-3.1-flash", "deepseek-chat"]


@pytest.mark.asyncio
async def test_gateway_skips_stale_glm_candidate_for_deepseek_endpoint(monkeypatch, reset_cloud_settings):
    settings.economy_cloud_llms = "glm-4.7,deepseek"
    settings.balanced_cloud_llms = ""
    settings.reasoning_cloud_llms = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_provider = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = ""
    settings.gemini_api_key = ""
    settings.gemini_model = ""
    settings.deepseek_api_key = "deepseek-key"
    settings.deepseek_model = "deepseek-v4-pro"
    settings.deepseek_base_url = "https://api.deepseek.com"
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    calls: list[str] = []

    async def fake_cloud_complete(prompt: str, **kwargs):
        model = kwargs.get("model_override") or ""
        calls.append(str(model))
        if model == "glm-4.7":
            raise AssertionError("stale GLM model must not be sent to DeepSeek endpoint")
        return "deepseek ok"

    monkeypatch.setattr("app.services.cloud_llm.cloud_complete", fake_cloud_complete)

    gateway = CloudLLMGateway()
    monkeypatch.setattr(gateway, "_known_model_available", lambda model_id: True)
    result = await gateway.generate(
        "Write a short architecture note.",
        task_type="architecture",
        mode="economy",
        allow_local_fallback=False,
    )

    assert result == "deepseek ok"
    assert calls == ["deepseek-v4-pro"]


@pytest.mark.asyncio
async def test_gateway_falls_back_from_ollama_to_lmstudio(monkeypatch, reset_cloud_settings):
    settings.local_llm_provider = "auto"
    settings.local_llm_fallback_order = "ollama,lmstudio"
    settings.lmstudio_model = "local-lmstudio"

    gateway = CloudLLMGateway()

    class _FakeOllama:
        async def generate(self, *args, **kwargs):
            return ""

    class _FakeLMStudio:
        async def generate(self, *args, **kwargs):
            return "lmstudio ok"

    gateway._local_service = _FakeOllama()
    gateway._lmstudio_service = _FakeLMStudio()

    result = await gateway._generate_local(prompt="Summarize locally.", timeout=60.0)

    assert result == "lmstudio ok"


@pytest.mark.asyncio
async def test_gateway_can_force_lmstudio_local_provider(monkeypatch, reset_cloud_settings):
    settings.local_llm_provider = "lmstudio"

    gateway = CloudLLMGateway()

    class _FakeOllama:
        async def generate(self, *args, **kwargs):
            raise AssertionError("Ollama should not be used")

    class _FakeLMStudio:
        async def generate(self, *args, **kwargs):
            return "forced lmstudio"

    gateway._local_service = _FakeOllama()
    gateway._lmstudio_service = _FakeLMStudio()

    result = await gateway._generate_local(prompt="Summarize locally.", timeout=60.0)

    assert result == "forced lmstudio"
