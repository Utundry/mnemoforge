from __future__ import annotations

from pathlib import Path

from scripts.public_release_config import render_public_env, validate_public_env


def test_render_public_env_uses_public_safe_defaults():
    text = render_public_env({"SELF_PROJECT_ID": "alpha", "API_KEY": "public-key"})

    assert "SELF_PROJECT_ID=alpha" in text
    assert "API_KEY=public-key" in text
    assert "OPENAI_API_KEY" not in text
    assert "DEEPSEEK_API_KEY" not in text


def test_validate_public_env_rejects_forbidden_internal_keys():
    report = validate_public_env(
        "\n".join(
            [
                "SELF_PROJECT_ID=alpha",
                "DISABLED_MODULES=layout_fixer",
                "API_KEY=",
                "OPENAI_API_KEY=secret",
            ]
        )
    )

    assert report["missing_required"] == []
    assert "OPENAI_API_KEY" in report["forbidden_present"]
