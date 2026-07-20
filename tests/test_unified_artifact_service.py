from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.unified_artifact import to_unified_status
from app.services.unified_artifact_service import UnifiedArtifactService


class FakeImprovementsStore:
    async def list(self, **kwargs):
        return []

    async def get(self, improvement_id):
        return None


class FakeTasksStore:
    def __init__(self, rows):
        self.rows = rows
        self.get_by_task_id_calls = 0

    def list_tasks(self, *, project=None, status=None, limit=50, **kwargs):
        return [
            row
            for row in self.rows
            if row["project"] == project and (status is None or row["status"] == status)
        ][:limit]

    def get_task_by_task_id(self, *, project, task_id):
        self.get_by_task_id_calls += 1
        return next(
            (
                row
                for row in self.rows
                if row["project"] == project and row["task_id"] == task_id
            ),
            None,
        )

    def get_unique_task_by_uuid(self, *, task_id):
        return None

    def list_changes(self, *, project=None, task_id=None, limit=100):
        return []


def _task_row(*, project: str, task_id: str, status: str, updated_at: float) -> dict:
    return {
        "id": str(uuid4()),
        "task_id": task_id,
        "project": project,
        "title": f"Task {task_id}",
        "description": "Task description",
        "agent_id": "codex",
        "status": status,
        "source": "test",
        "tags": [],
        "topic_path": None,
        "linked_improvement_id": None,
        "created_at": updated_at - 10,
        "updated_at": updated_at,
    }


@pytest.mark.asyncio
async def test_list_task_artifacts_batches_capture_summary(monkeypatch):
    rows = [
        _task_row(project="alpha", task_id="task-1", status="planning", updated_at=100.0),
        _task_row(project="alpha", task_id="task-2", status="active", updated_at=200.0),
        _task_row(project="alpha", task_id="task-3", status="planning", updated_at=300.0),
    ]
    tasks_store = FakeTasksStore(rows)
    service = UnifiedArtifactService()
    service._improvements_store = FakeImprovementsStore()
    service._tasks_store = tasks_store
    summary_calls = []

    async def fake_summary_map(project: str, *, limit_hint: int = 200):
        summary_calls.append((project, limit_hint))
        return {
            "task-1": {
                "task_capture_pending_count": 2,
                "task_capture_promoted_count": 1,
                "task_statement_incomplete": True,
            }
        }

    monkeypatch.setattr(
        "app.services.unified_artifact_service._task_capture_summary_map",
        fake_summary_map,
    )

    result = await service.list_artifacts(
        project="alpha",
        status="open",
        type_="task",
        limit=3,
    )

    assert summary_calls == [("alpha", 100)]
    assert tasks_store.get_by_task_id_calls == 0
    assert result.backend_used == "sqlite_lexical"
    assert [item.task_id for item in result.items] == ["task-3", "task-2", "task-1"]
    task_1 = next(item for item in result.items if item.task_id == "task-1")
    assert task_1.status == to_unified_status("task", "planning")
    assert task_1.task_capture_pending_count == 2
    assert task_1.task_capture_promoted_count == 1
    assert task_1.task_statement_incomplete is True
