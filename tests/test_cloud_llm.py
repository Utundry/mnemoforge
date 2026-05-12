from __future__ import annotations

import json
import httpx
import pytest

from app.config import settings
from app.services.cloud_llm import available_cloud_models, cloud_available, cloud_complete, cloud_provider, is_cloud_model_callable


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
        "cloud_llm_model_profiles": settings.cloud_llm_model_profiles,
        "disabled_cloud_llms": settings.disabled_cloud_llms,
    }
    settings.cloud_llm_model_profiles = ""
    settings.disabled_cloud_llms = ""
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


@pytest.mark.asyncio
async def test_cloud_complete_uses_native_gemini_generate_content_when_api_style_requests_it(
    monkeypatch,
    reset_cloud_settings,
):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = ""
    settings.glm_api_key = ""

    monkeypatch.setenv(
        "CLOUD_LLM_MODEL_PROFILES",
        json.dumps(
            {
                "gemini-3-flash-preview": {
                    "provider": "gemini",
                    "api_style": "gemini-native",
                    "api_key": "gem-key",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                    "model": "gemini-3-flash-preview",
                }
            }
        ),
    )

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
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "AI uses patterns."}
                            ]
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await cloud_complete(
        "Explain how AI works in a few words",
        system="Be concise.",
        max_tokens=123,
        temperature=0.4,
        model_override="gemini-3-flash-preview",
    )

    assert result == "AI uses patterns."
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"
    assert captured["headers"] == {
        "x-goog-api-key": "gem-key",
        "Content-Type": "application/json",
    }
    assert captured["json"] == {
        "contents": [
            {
                "parts": [
                    {
                        "text": "Be concise.\n\nExplain how AI works in a few words"
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 123,
        },
    }


@pytest.mark.asyncio
async def test_cloud_complete_uses_profile_specific_endpoint_for_model_override(
    monkeypatch,
    reset_cloud_settings,
):
    settings.cloud_llm_provider = "gemini"
    settings.cloud_llm_api_key = "gem-key"
    settings.cloud_llm_model = "gemini-2.5-flash"
    settings.cloud_llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    settings.glm_api_key = "legacy-key"
    settings.glm_model = "glm-4.5-air"
    settings.glm_base_url = "https://api.z.ai/api/paas/v4"

    captured: dict[str, object] = {}

    async def fake_post(self, url, *, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "glm reply"}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await cloud_complete(
        "Continue on alternative model",
        model_override="glm-4.5-air",
    )

    assert result == "glm reply"
    assert captured["url"] == "https://api.z.ai/api/paas/v4/chat/completions"
    assert captured["headers"] == {
        "Authorization": "legacy-key",
        "Content-Type": "application/json",
    }
    assert captured["json"]["model"] == "glm-4.5-air"


def test_available_cloud_models_includes_profiled_env_entries(monkeypatch, reset_cloud_settings):
    settings.cloud_llm_provider = "gemini"
    settings.cloud_llm_api_key = "gem-key"
    settings.cloud_llm_model = "gemini-2.5-flash"
    settings.cloud_llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    settings.glm_api_key = ""
    settings.gemini_api_key = ""
    settings.gemini_model = ""
    settings.deepseek_api_key = ""

    monkeypatch.setenv(
        "CLOUD_LLM_MODEL_PROFILES",
        json.dumps(
            {
                "gpt-4o-mini": {
                    "provider": "openai",
                    "api_style": "openai-chat",
                    "api_key": "openai-key",
                    "base_url": "https://api.openai.com/v1",
                }
            }
        ),
    )

    assert cloud_available() is True
    assert available_cloud_models() == ["gemini-2.5-flash", "gpt-4o-mini"]
    assert cloud_provider(model_override="gpt-4o-mini") == "openai:gpt-4o-mini"


def test_available_cloud_models_includes_first_class_gemini_settings(reset_cloud_settings):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = "glm-key"
    settings.glm_model = "glm-4.7"
    settings.gemini_api_key = "gem-key"
    settings.gemini_model = "gemini-3-flash-preview"
    settings.gemini_base_url = "https://generativelanguage.googleapis.com/v1beta"
    settings.deepseek_api_key = ""

    assert available_cloud_models() == ["glm-4.7", "gemini-3-flash-preview"]
    assert cloud_provider(model_override="gemini-3-flash-preview") == "gemini:gemini-3-flash-preview"


def test_available_cloud_models_filters_inactive_profile_entries(monkeypatch, reset_cloud_settings):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = "glm-key"
    settings.glm_model = "glm-4.7"
    settings.gemini_api_key = "gem-key"
    settings.gemini_model = "gemini-3-flash-preview"
    settings.disabled_cloud_llms = ""

    monkeypatch.setenv(
        "CLOUD_LLM_MODEL_PROFILES",
        json.dumps(
            {
                "deepseek-chat": {
                    "provider": "deepseek",
                    "api_style": "openai-chat",
                    "api_key": "deepseek-key",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                    "enabled": True,
                },
                "glm-4.7": {
                    "provider": "glm",
                    "model": "glm-4.7",
                    "enabled": False,
                },
                "gemini-3-flash-preview": {
                    "provider": "gemini",
                    "model": "gemini-3-flash-preview",
                    "active": False,
                }
            }
        ),
    )

    assert available_cloud_models() == ["deepseek-chat"]
    assert cloud_provider(model_override="deepseek-chat") == "deepseek:deepseek-chat"


def test_profile_api_key_env_can_read_settings_loaded_from_dotenv(reset_cloud_settings):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = ""
    settings.gemini_api_key = ""
    settings.gemini_model = ""
    settings.deepseek_api_key = "deepseek-key"
    settings.cloud_llm_model_profiles = json.dumps(
        {
            "deepseek-chat": {
                "provider": "deepseek",
                "api_style": "openai-chat",
                "api_key_env": "DEEPSEEK_API_KEY",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "enabled": True,
            }
        }
    )

    assert available_cloud_models() == ["deepseek-chat"]


def test_available_cloud_models_includes_first_class_deepseek_settings(reset_cloud_settings):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = ""
    settings.gemini_api_key = ""
    settings.gemini_model = ""
    settings.deepseek_api_key = "deepseek-key"
    settings.deepseek_model = "deepseek-chat"
    settings.deepseek_base_url = "https://api.deepseek.com"

    assert cloud_available() is True
    assert available_cloud_models() == ["deepseek-chat"]
    assert cloud_provider(model_override="deepseek-chat") == "deepseek:deepseek-chat"


def test_deepseek_config_rejects_stale_glm_override_before_http(reset_cloud_settings):
    settings.cloud_llm_provider = ""
    settings.cloud_llm_api_key = ""
    settings.cloud_llm_model = ""
    settings.glm_api_key = ""
    settings.gemini_api_key = ""
    settings.gemini_model = ""
    settings.deepseek_api_key = "deepseek-key"
    settings.deepseek_model = "deepseek-v4-pro"
    settings.deepseek_base_url = "https://api.deepseek.com"

    assert is_cloud_model_callable("deepseek-v4-pro") is True
    assert is_cloud_model_callable("deepseek-v4-flash") is True
    assert is_cloud_model_callable("glm-4.7") is False
