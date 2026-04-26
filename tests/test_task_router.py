from __future__ import annotations

import pytest

from app.services import task_router


def test_heuristic_classify_prefers_code_generation_for_bounded_engineering_packet():
    task = "Bounded MCP packet for parity and focused tests in app/routers/mcp_sse.py and mcp/server.py"
    assert task_router._heuristic_classify(task) == "code_generation"


@pytest.mark.asyncio
async def test_decide_falls_back_to_heuristic_when_llm_classification_fails(monkeypatch):
    async def _boom(task: str) -> str:
        raise RuntimeError("ollama unavailable")

    monkeypatch.setattr(task_router, "_llm_classify", _boom)

    decision = await task_router.decide(
        task="Implement API endpoint wiring in app/routers/models.py",
        preferred_tier="cloud",
    )

    assert decision.task_type == "code_generation"
    assert decision.tier == "cloud"


def test_route_filters_ghost_components_from_reasoning_and_alternatives(monkeypatch):
    class _FakeCapabilityRegistry:
        def best_for(self, task_type: str):
            return [
                ("claude-sonnet", 0.95),
                ("gpt-4o", 0.91),
                ("qwen3:1.7b", 0.72),
                ("skill:cached-review", 0.65),
                ("cloud-llm", 0.60),
            ]

    class _FakeModelRegistry:
        def rank_for_task(self, task_type: str):
            return [("glm-4.7", 0.84), ("gemini-3.1-flash", 0.79)]

    monkeypatch.setattr(task_router, "get_registry", lambda: _FakeCapabilityRegistry())

    import app.services.model_registry as model_registry

    monkeypatch.setattr(model_registry, "get_model_registry", lambda: _FakeModelRegistry())

    decision = task_router._route("code_generation", preferred_tier="cloud")

    assert decision.component == "glm-4.7"
    assert decision.tier == "cloud"
    assert "claude-sonnet" not in decision.reasoning
    assert "gpt-4o" not in decision.reasoning
    assert all(component not in {"claude-sonnet", "gpt-4o"} for component, _score in decision.alternatives)
    assert "qwen3:1.7b" in decision.reasoning
