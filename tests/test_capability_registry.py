from __future__ import annotations

import json

from app.services.capability_registry import CapabilityRegistry


def test_capability_registry_hides_ghost_models_from_components_and_best_for(tmp_path, monkeypatch):
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps(
            {
                "qwen3:1.7b": {
                    "code_generation": {"success": 3, "fail": 1, "description": "Local model"},
                },
                "glm-4.7": {
                    "code_generation": {"success": 8, "fail": 1, "description": "Cloud model"},
                },
                "claude-sonnet": {
                    "code_generation": {"success": 9, "fail": 0, "description": "Ghost model"},
                },
                "skill:bounded-patch": {
                    "code_generation": {"success": 5, "fail": 0, "description": "Cached skill"},
                },
            }
        ),
        encoding="utf-8",
    )

    class _FakeModelRegistry:
        _models = {"glm-4.7": {"model_id": "glm-4.7"}}

    import app.services.model_registry as model_registry

    monkeypatch.setattr(model_registry, "get_model_registry", lambda: _FakeModelRegistry())

    registry = CapabilityRegistry(path)

    components = registry.components()
    ranked = registry.best_for("code_generation")

    assert "claude-sonnet" not in components
    assert "qwen3:1.7b" in components
    assert "glm-4.7" in components
    assert "skill:bounded-patch" in components
    assert all(component != "claude-sonnet" for component, _score in ranked)
