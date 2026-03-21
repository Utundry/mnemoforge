from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.services.cloud_llm import cloud_available, cloud_complete, cloud_provider


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
    }
    yield
    for key, value in snapshot.items():
        setattr(settings, key, value)


def test_cloud_provider_prefers_generic_gemini_config(reset_cloud_settings):
    settings.cloud_llm_provider = "gemini"
    settings.cloud_llm_api_key = "gem-key"
    settings.cloud_llm_model = "gemini-2.5-flash"
    settings.cloud_llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    settings.glm_api_key = "legacy-key"

    assert cloud_available() is True
    assert cloud_provider() == "gemini:gemini-2.5-flash"


def test_cloud_provider_falls_back_to_legacy_glm(reset_cloud_settings):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = ""
    settings.glm_api_key = "legacy-key"
    settings.glm_model = "glm-4.5-air"
    settings.glm_base_url = "https://api.z.ai/api/coding/paas/v4"

    assert cloud_available() is True
    assert cloud_provider() == "glm:glm-4.5-air"


@pytest.mark.asyncio
async def test_cloud_complete_uses_configured_openai_compatible_endpoint(
    monkeypatch,
    reset_cloud_settings,
):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = "gem-key"
    settings.cloud_llm_model = "gemini-2.5-flash"
    settings.cloud_llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    settings.glm_api_key = ""

    captured: dict[str, object] = {}

    async def fake_post(self, url, *, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": [
                                {"type": "output_text", "text": "Gemini fallback reply"}
                            ],
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await cloud_complete(
        "Explain the migration",
        system="You are a migration assistant.",
        max_tokens=321,
        temperature=0.1,
    )

    assert result == "Gemini fallback reply"
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer gem-key",
        "Content-Type": "application/json",
    }
    assert captured["json"] == {
        "model": "gemini-2.5-flash",
        "messages": [
            {"role": "system", "content": "You are a migration assistant."},
            {"role": "user", "content": "Explain the migration"},
        ],
        "max_tokens": 321,
        "temperature": 0.1,
    }
