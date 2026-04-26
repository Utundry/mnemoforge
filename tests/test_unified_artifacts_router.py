from __future__ import annotations

import pytest
import pytest_asyncio

from tests.test_living_docs_regressions import _FakeQueue, _build_client_with_queue


@pytest_asyncio.fixture
async def client_with_unified_artifacts():
    fake_queue = _FakeQueue()
    client = await _build_client_with_queue(fake_queue)
    async with client:
        yield client, fake_queue

    qdrant_client = getattr(client, "_qdrant_client", None)
    if qdrant_client is not None:
        await qdrant_client.close()


@pytest.mark.asyncio
async def test_list_artifacts_accepts_status_query_alias(client_with_unified_artifacts) -> None:
    client, _fake_queue = client_with_unified_artifacts

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Router status alias improvement",
            "description": "Should be filtered via status alias.",
            "project": "proj-router",
            "agent_id": "architect",
            "importance_score": 0.8,
            "tags": ["test"],
        },
    )
    assert create.status_code == 201, create.text
    improvement_id = create.json()["id"]

    resolve = await client.patch(
        f"/api/v1/improvements/{improvement_id}/resolve",
        json={
            "acted_by": "owner",
            "action_source": "test",
            "reason": "Resolve to verify filtering.",
        },
    )
    assert resolve.status_code == 200, resolve.text

    open_result = await client.get(
        "/api/v1/artifacts",
        params={"project": "proj-router", "status": "open", "limit": 50},
    )
    assert open_result.status_code == 200, open_result.text
    assert open_result.json()["items"] == []

    done_result = await client.get(
        "/api/v1/artifacts",
        params={"project": "proj-router", "status": "done", "limit": 50},
    )
    assert done_result.status_code == 200, done_result.text
    assert len(done_result.json()["items"]) == 2
    assert {item["status"] for item in done_result.json()["items"]} == {"done"}


@pytest.mark.asyncio
async def test_resolve_artifact_survives_best_effort_followup_failure(
    client_with_unified_artifacts,
    monkeypatch,
) -> None:
    client, fake_queue = client_with_unified_artifacts

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Router follow-up failure improvement",
            "description": "Should still resolve when follow-up work fails.",
            "project": "proj-router",
            "agent_id": "architect",
            "importance_score": 0.8,
            "tags": ["test"],
        },
    )
    assert create.status_code == 201, create.text
    improvement_id = create.json()["id"]

    async def _noop_ensure_task_for_improvement(*args, **kwargs):
        return None

    async def _boom_record_improvement_task_change(*args, **kwargs):
        raise RuntimeError("simulated follow-up failure")

    monkeypatch.setattr(
        "app.services.project_task_service.ensure_task_for_improvement",
        _noop_ensure_task_for_improvement,
    )
    monkeypatch.setattr(
        "app.services.project_task_service.record_improvement_task_change",
        _boom_record_improvement_task_change,
    )

    resolve = await client.post(
        f"/api/v1/artifacts/improvement:proj-router:{improvement_id}/resolve",
        json={
            "acted_by": "owner",
            "action_source": "test",
            "reason": "Resolve despite follow-up failure.",
        },
    )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["status"] == "done"

    fetched = await client.get(f"/api/v1/artifacts/improvement:proj-router:{improvement_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "done"

    assert ("task_memoir", {"task_id": improvement_id, "project": "proj-router"}) in fake_queue.calls
    assert ("docs_rebuild", {"project": "proj-router"}) in fake_queue.calls
    assert ("rebuild_project_tasks", {"project": "proj-router", "_queue_lane": "slow"}) in fake_queue.calls
