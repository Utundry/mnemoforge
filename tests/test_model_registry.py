from __future__ import annotations

import json

from app.services.cloud_llm import CloudLLMConfig
from app.services.model_registry import ModelRegistry


def test_model_registry_bootstraps_from_configured_profiles_and_prunes_legacy_entries(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "model_registry.json"
    db_path = tmp_path / "quota.db"
    config_path.write_text(
        json.dumps(
            {
                "claude-sonnet": {
                    "model_id": "claude-sonnet",
                    "display_name": "Claude Sonnet",
                    "provider": "anthropic",
                    "daily_limit": 100000,
                    "limit_unit": "tokens",
                    "priority": 1,
                },
                "codex": {
                    "model_id": "codex",
                    "display_name": "OpenAI Codex CLI",
                    "provider": "openai",
                    "daily_limit": 500000,
                    "limit_unit": "tokens",
                    "priority": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.services.model_registry.configured_cloud_model_profiles",
        lambda: {
            "glm-4.5-air": CloudLLMConfig(
                provider="glm",
                api_key="glm-key",
                model="glm-4.5-air",
                base_url="https://api.z.ai/api/paas/v4",
            ),
            "gemini-2.5-flash": CloudLLMConfig(
                provider="gemini",
                api_key="gem-key",
                model="gemini-2.5-flash",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            ),
        },
    )

    registry = ModelRegistry(config_path=config_path, db_path=db_path)

    assert list(registry._models) == ["glm-4.5-air", "gemini-2.5-flash"]
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert list(persisted) == ["glm-4.5-air", "gemini-2.5-flash"]
    assert persisted["glm-4.5-air"]["managed_by"] == "config"
    assert persisted["gemini-2.5-flash"]["managed_by"] == "config"

    registry.close()


def test_model_registry_preserves_manual_entries_alongside_configured_profiles(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "model_registry.json"
    db_path = tmp_path / "quota.db"
    config_path.write_text(
        json.dumps(
            {
                "manual-reviewer": {
                    "model_id": "manual-reviewer",
                    "display_name": "Manual Reviewer",
                    "provider": "internal",
                    "daily_limit": 12345,
                    "limit_unit": "tokens",
                    "priority": 9,
                    "task_capabilities": ["code_review"],
                    "managed_by": "manual",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.services.model_registry.configured_cloud_model_profiles",
        lambda: {
            "glm-4.5-air": CloudLLMConfig(
                provider="glm",
                api_key="glm-key",
                model="glm-4.5-air",
                base_url="https://api.z.ai/api/paas/v4",
            )
        },
    )

    registry = ModelRegistry(config_path=config_path, db_path=db_path)

    assert set(registry._models) == {"manual-reviewer", "glm-4.5-air"}
    assert registry._models["manual-reviewer"]["managed_by"] == "manual"
    assert registry._models["glm-4.5-air"]["managed_by"] == "config"

    registry.close()
