from __future__ import annotations

from types import SimpleNamespace

from app.services.task_capture_rules import compute_task_statement_missing_artifacts


def test_task_statement_missing_artifacts_requires_task_checkpoint_for_active_work():
    missing = compute_task_statement_missing_artifacts(
        title="Checkpoint rule",
        description="Task without a checkpoint should be flagged.",
        status="active",
        changes=[
            SimpleNamespace(
                content="Implemented the bounded slice.",
                why="Still needs a durable checkpoint.",
                change_type="implementation",
                tags=[],
            )
        ],
    )

    assert "task_checkpoint" in missing


def test_task_statement_missing_artifacts_accepts_task_checkpoint_tag():
    missing = compute_task_statement_missing_artifacts(
        title="Checkpoint rule",
        description="Task with a checkpoint should not be flagged for that artifact.",
        status="active",
        changes=[
            SimpleNamespace(
                content="[task_checkpoint]\nCheckpoint stage: planning\nCheckpoint status: planning\nSummary: Framed the task.",
                why="Recorded before the first implementation slice.",
                change_type="note",
                tags=["task_checkpoint", "task_stage:planning", "task_status:planning"],
            )
        ],
    )

    assert "task_checkpoint" not in missing
