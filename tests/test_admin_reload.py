from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_soft_reload_clears_workflow_spec_caches(monkeypatch) -> None:
    from app.routers import admin
    import app.dependencies as dependencies

    class FakeOllama:
        async def embed(self, text: str) -> list[float]:
            return [1.0]

    monkeypatch.setattr(admin.settings, "qdrant_in_memory", True)
    monkeypatch.setattr(dependencies, "get_ollama", lambda: FakeOllama())

    result = await admin.soft_reload(None)

    workflow_specs = result["results"]["workflow_specs"]
    assert result["status"] == "reloaded"
    assert workflow_specs["status"] == "ok"
    assert "app.services.mcp_workflow_specs.load_named_json_spec" in workflow_specs["cleared"]
