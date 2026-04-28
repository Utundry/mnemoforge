import pytest

from app.services.memory_scribe_service import compact_memory_scribe, draft_task_checkpoint


@pytest.mark.asyncio
async def test_memory_scribe_compacts_raw_notes_without_mutating_memory():
    result = await compact_memory_scribe(
        {
            "project": "alpha",
            "task_id": "task-1",
            "task_title": "Improve checkpoints",
            "stage": "handoff",
            "status": "active",
            "use_llm": False,
            "raw_notes": "\n".join(
                [
                    "Summary: Added a checkpoint draft helper.",
                    "Decisions: Keep the first scribe slice review-only.",
                    "Changed files: app/services/memory_scribe_service.py; app/routers/tasks.py",
                    "Verification: Focused unit tests cover deterministic fallback.",
                    "Remaining risk: LLM extraction quality still needs live feedback.",
                    "Next step: Wire the job into the API queue.",
                ]
            ),
        },
        llm_gateway=None,
    )

    assert result["draft"]["summary"] == "Added a checkpoint draft helper."
    assert result["draft"]["decisions"] == ["Keep the first scribe slice review-only."]
    assert result["draft"]["changed_files"] == ["app/services/memory_scribe_service.py", "app/routers/tasks.py"]
    assert result["draft"]["verification"] == ["Focused unit tests cover deterministic fallback."]
    assert result["draft"]["remaining_risk"] == ["LLM extraction quality still needs live feedback."]
    assert result["draft"]["next_step"] == "Wire the job into the API queue."
    assert result["quality_gate"]["status"] == "ready"
    assert result["quality_gate"]["can_autofill_checkpoint"] is True
    assert result["scribe"]["mutates_memory"] is False
    assert "[task_checkpoint]" in result["checkpoint_content"]
    assert result["token_budget"]["basis"] == "model_context_window_ratio"


@pytest.mark.asyncio
async def test_memory_scribe_quality_gate_requires_review_for_sparse_notes():
    result = await compact_memory_scribe(
        {
            "project": "alpha",
            "task_id": "task-1",
            "use_llm": False,
            "raw_notes": "Worked on it a bit.",
        },
        llm_gateway=None,
    )

    assert result["quality_gate"]["status"] == "needs_review"
    assert "verification" in result["quality_gate"]["missing"]
    assert "next_step" in result["quality_gate"]["missing"]
    assert result["quality_gate"]["requires_reasoning_model_review"] is True


@pytest.mark.asyncio
async def test_draft_task_checkpoint_blocks_ungrounded_files_and_tests():
    class FakeGateway:
        async def generate(self, *args, **kwargs):
            return """
            {
              "summary": "Captured the scribe MCP draft path.",
              "changed_files": ["app/routers/mcp_sse.py", "app/secret_fake.py"],
              "verification": ["pytest tests/test_mcp_sse.py passed", "npm test passed"],
              "next_step": "Review the generated checkpoint args."
            }
            """

    result = await draft_task_checkpoint(
        {
            "project": "alpha",
            "task_id": "task-2",
            "stage": "in_progress",
            "raw_notes": "\n".join(
                [
                    "Summary: Captured the scribe MCP draft path.",
                    "Changed files: app/routers/mcp_sse.py",
                    "Verification: pytest tests/test_mcp_sse.py passed",
                    "Next step: Review the generated checkpoint args.",
                ]
            ),
        },
        llm_gateway=FakeGateway(),
    )

    assert result["mutates_memory"] is False
    assert result["recommended_next_tool"] == "record_task_checkpoint"
    assert result["record_task_checkpoint_args"]["changed_files"] == ["app/routers/mcp_sse.py"]
    assert result["record_task_checkpoint_args"]["verification"] == ["pytest tests/test_mcp_sse.py passed"]
    assert result["validation_report"]["status"] == "needs_review"
    assert result["validation_report"]["blocked_ungrounded"]["changed_files"] == ["app/secret_fake.py"]
    assert result["validation_report"]["blocked_ungrounded"]["verification"] == ["npm test passed"]
