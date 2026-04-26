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
