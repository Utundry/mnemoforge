from __future__ import annotations

import pytest

from app.services.data_integrity_autorepair_service import (
    build_integrity_autorepair_decision,
    integrity_autorepair_enabled,
    load_integrity_autorepair_policy,
    maybe_queue_integrity_autorepairs,
)
from app.services.data_integrity_service import (
    TASK_MEMOIR_TAG_FILTER_SLICE_ID,
    get_data_integrity_store,
)


class FakeQueue:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.submissions: list[dict[str, object]] = []

    def register(self, job_type: str, handler: object) -> None:
        self.handlers[job_type] = handler

    async def submit(self, job_type: str, payload: dict[str, object]) -> str:
        self.submissions.append({"job_type": job_type, "payload": payload})
        return f"job-{len(self.submissions)}"


def _mark_task_memoir_actionable() -> None:
    store = get_data_integrity_store()
    store.upsert_slice(
        slice_id=TASK_MEMOIR_TAG_FILTER_SLICE_ID,
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="task_memoir tag filter degraded",
    )
    store.upsert_finding(
        finding_id="task-memoir-autorepair-finding",
        slice_id=TASK_MEMOIR_TAG_FILTER_SLICE_ID,
        category="task_memoir",
        record_id="task-1",
        suspicion_type="missing_memoir_tag",
        confidence=0.9,
        source="test",
        details={"suggested_repair": "qdrant_reindex_from_sqlite"},
    )


def test_integrity_autorepair_policy_loads_and_enables_default(monkeypatch):
    monkeypatch.delenv("INTEGRITY_AUTO_REMEDIATE", raising=False)

    policy = load_integrity_autorepair_policy()

    assert policy["enabled"] is True
    assert integrity_autorepair_enabled(policy) is True
    assert TASK_MEMOIR_TAG_FILTER_SLICE_ID in policy["slices"]
    assert policy["slices"][TASK_MEMOIR_TAG_FILTER_SLICE_ID]["action_type"] == "qdrant_reindex_from_sqlite"


def test_integrity_autorepair_decision_blocks_unsafe_policy_action(monkeypatch):
    monkeypatch.delenv("INTEGRITY_AUTO_REMEDIATE", raising=False)
    _mark_task_memoir_actionable()
    policy = load_integrity_autorepair_policy()
    policy["slices"][TASK_MEMOIR_TAG_FILTER_SLICE_ID]["action_type"] = "delete_canonical_records"

    decision = build_integrity_autorepair_decision(TASK_MEMOIR_TAG_FILTER_SLICE_ID, policy=policy)

    assert decision["allowed"] is False
    assert decision["reason"] == "unsafe_action_type"


@pytest.mark.asyncio
async def test_integrity_autorepair_queues_safe_qdrant_reindex_once(monkeypatch):
    monkeypatch.delenv("INTEGRITY_AUTO_REMEDIATE", raising=False)
    _mark_task_memoir_actionable()
    queue = FakeQueue()

    result = await maybe_queue_integrity_autorepairs(
        queue=queue,
        discovery_limit=10,
        discovery_cooldown_seconds=3600.0,
        remediation_cooldown_seconds=0.0,
    )

    assert len(result["queued"]) == 1
    assert result["queued"][0]["slice_id"] == TASK_MEMOIR_TAG_FILTER_SLICE_ID
    assert queue.submissions == [
        {
            "job_type": "qdrant_reindex_from_sqlite",
            "payload": {"targets": ["task_memoir"], "limit": 100, "record_ids": ["task-1"]},
        }
    ]
    assert "qdrant_reindex_from_sqlite" in queue.handlers

    second = await maybe_queue_integrity_autorepairs(
        queue=queue,
        discovery_limit=10,
        discovery_cooldown_seconds=3600.0,
        remediation_cooldown_seconds=0.0,
    )

    assert second["queued"] == []
    assert second["skipped"][0]["reason"] == "active_remediation_exists"
    assert len(queue.submissions) == 1


def test_integrity_autorepair_respects_attempt_limit(monkeypatch):
    monkeypatch.delenv("INTEGRITY_AUTO_REMEDIATE", raising=False)
    _mark_task_memoir_actionable()
    store = get_data_integrity_store()
    policy = load_integrity_autorepair_policy()
    policy["defaults"]["max_auto_attempts"] = 2

    for index in range(2):
        remediation_id = f"auto-remediation-{index}"
        store.queue_remediation(
            remediation_id=remediation_id,
            slice_id=TASK_MEMOIR_TAG_FILTER_SLICE_ID,
            action_type="qdrant_reindex_from_sqlite",
            requested_by="auto_integrity",
            job_id=f"job-done-{index}",
            details={"description": "test"},
        )
        store.sync_remediation_status(remediation_id=remediation_id, status="done")

    decision = build_integrity_autorepair_decision(
        TASK_MEMOIR_TAG_FILTER_SLICE_ID,
        policy=policy,
        cooldown_seconds=0.0,
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "attempt_limit_reached"
    assert decision["attempts"] == 2

