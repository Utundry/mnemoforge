from __future__ import annotations

from argparse import Namespace

from scripts.configure_public import render_user_env


def _args(**overrides):
    values = {
        "project_id": None,
        "http_port": None,
        "api_key": "",
        "cloud_provider": None,
        "cloud_api_key": "",
        "cloud_model": "",
        "cloud_base_url": "",
        "no_local_llm": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_render_user_env_uses_safe_first_user_defaults():
    text = render_user_env(_args(), interactive=False)

    assert "SELF_PROJECT_ID=mnemoforge" in text
    assert "SERVER_PORT=8000" in text
    assert "MNEMOFORGE_HTTP_PORT=8000" in text
    assert "MNEMOFORGE_USER_ENV_FILE=.env.user" in text
    assert "LLM_GATEWAY_ENABLE_LOCAL_FALLBACK=1" in text
    assert "DEEPSEEK_API_KEY" not in text


def test_render_user_env_can_configure_deepseek_generic_provider():
    text = render_user_env(
        _args(
            cloud_provider="deepseek",
            cloud_api_key="secret",
            api_key="local-secret",
            http_port=8080,
        ),
        interactive=False,
    )

    assert "API_KEY=local-secret" in text
    assert "MNEMOFORGE_HTTP_PORT=8080" in text
    assert "CLOUD_LLM_PROVIDER=deepseek" in text
    assert "CLOUD_LLM_API_KEY=secret" in text
    assert "CLOUD_LLM_MODEL=deepseek-chat" in text
    assert "CLOUD_LLM_BASE_URL=https://api.deepseek.com" in text
