import asyncio
from pathlib import Path

from app.services import mcp_tool_registry as registry
from app.services.mcp_tool_registry import (
    bootstrap_tool_lifecycle,
    get_tool_stage,
    list_testing_tools,
    record_tool_feedback,
    review_due_tool_lifecycles,
)


def test_new_tool_is_auto_seeded_as_testing_after_catalog_bootstrap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "_DB_PATH", tmp_path / "mcp_tool_lifecycle.db")

    first = bootstrap_tool_lifecycle(["memory_store"])
    assert first["created"] == 1
    assert get_tool_stage("memory_store") == "stable"

    second = bootstrap_tool_lifecycle(["memory_store", "new_tool_surface"])
    assert second["created"] == 1
    assert get_tool_stage("new_tool_surface") == "testing"
    assert "new_tool_surface" in list_testing_tools()


def test_positive_feedback_can_promote_testing_tool_to_stable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "_DB_PATH", tmp_path / "mcp_tool_lifecycle.db")
    bootstrap_tool_lifecycle(["tool_feedback"])

    record_tool_feedback(
        tool_name="tool_feedback",
        valence="positive",
        tool_stage="testing",
        worked=True,
        friction="",
        suggestion="",
        task_context="",
        project_id="supermemory",
        agent_id="codex",
        session_id="sess-1",
    )
    record_tool_feedback(
        tool_name="tool_feedback",
        valence="positive",
        tool_stage="testing",
        worked=True,
    )
    record_tool_feedback(
        tool_name="tool_feedback",
        valence="positive",
        tool_stage="testing",
        worked=True,
    )

    result = asyncio.run(
        review_due_tool_lifecycles(
            tool_catalog=[{"name": "tool_feedback", "description": "Record feedback after testing tool use"}],
            ollama=None,
            min_age_days=0,
            max_age_days=0,
            min_feedback=3,
        )
    )

    assert result["promoted"] == 1
    assert get_tool_stage("tool_feedback") == "stable"


def test_negative_feedback_can_deprecate_testing_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "_DB_PATH", tmp_path / "mcp_tool_lifecycle.db")
    bootstrap_tool_lifecycle(["tool_feedback"])

    record_tool_feedback(tool_name="tool_feedback", valence="negative", tool_stage="testing", worked=False)
    record_tool_feedback(tool_name="tool_feedback", valence="negative", tool_stage="testing", worked=False)
    record_tool_feedback(tool_name="tool_feedback", valence="negative", tool_stage="testing", worked=False)

    result = asyncio.run(
        review_due_tool_lifecycles(
            tool_catalog=[{"name": "tool_feedback", "description": "Record feedback after testing tool use"}],
            ollama=None,
            min_age_days=0,
            max_age_days=0,
            min_feedback=3,
        )
    )

    assert result["deprecated"] == 1
    assert get_tool_stage("tool_feedback") == "deprecated"
