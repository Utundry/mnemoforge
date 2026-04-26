from __future__ import annotations

import pytest
import pytest_asyncio

from tests.test_living_docs_regressions import _FakeQueue, _build_client_with_queue


@pytest_asyncio.fixture
async def client_with_fake_queue_and_tasks():
    fake_queue = _FakeQueue()
    client = await _build_client_with_queue(fake_queue)
    async with client:
        yield client, fake_queue

    qdrant_client = getattr(client, "_qdrant_client", None)
    if qdrant_client is not None:
        await qdrant_client.close()


@pytest.mark.asyncio
async def test_improvement_bootstraps_canonical_task_and_records_changes(
    client_with_fake_queue_and_tasks,
) -> None:
    client, fake_queue = client_with_fake_queue_and_tasks

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Unify project context retrieval",
            "description": "Assemble task context from governed knowledge entities.",
            "project": "proj-knowledge",
            "agent_id": "architect",
            "importance_score": 0.8,
            "tags": ["autodocs", "knowledge-first"],
        },
    )
    assert create.status_code == 201, create.text
    improvement = create.json()
    improvement_id = improvement["id"]

    task = await client.get(f"/api/v1/project/tasks/{improvement_id}?project=proj-knowledge")
    assert task.status_code == 200, task.text
    task_body = task.json()
    assert task_body["task_id"] == improvement_id
    assert task_body["linked_improvement_id"] == improvement_id
    assert task_body["title"] == "Unify project context retrieval"
    assert task_body["status"] == "planning"

    changes = await client.get(f"/api/v1/project/tasks/{improvement_id}/changes?project=proj-knowledge")
    assert changes.status_code == 200, changes.text
    change_items = changes.json()
    assert len(change_items) == 1
    assert change_items[0]["change_type"] == "task_created"
    assert "Task bootstrapped from improvement" in change_items[0]["content"]

    resolve = await client.patch(
        f"/api/v1/improvements/{improvement_id}/resolve",
        json={
            "acted_by": "owner",
            "action_source": "inline_user_approval",
            "reason": "Canonical task and memoir path verified.",
        },
    )
    assert resolve.status_code == 200, resolve.text

    resolved_task = await client.get(f"/api/v1/project/tasks/{improvement_id}?project=proj-knowledge")
    assert resolved_task.status_code == 200, resolved_task.text
    resolved_task_body = resolved_task.json()
    assert resolved_task_body["status"] == "done"
    assert len(resolved_task_body["changes"]) == 2
    assert resolved_task_body["changes"][1]["change_type"] == "status_change"
    assert resolved_task_body["changes"][1]["source"] == "inline_user_approval"

    assert ("task_memoir", {"task_id": improvement_id, "project": "proj-knowledge"}) in fake_queue.calls
    assert ("docs_rebuild", {"project": "proj-knowledge"}) in fake_queue.calls


@pytest.mark.asyncio
async def test_improvement_review_sets_stage_and_verdict(client_with_fake_queue_and_tasks) -> None:
    client, _ = client_with_fake_queue_and_tasks

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Surface beta-test quality",
            "description": "Keep stage and verdict separate from lifecycle status.",
            "project": "proj-knowledge",
            "agent_id": "architect",
            "importance_score": 0.7,
            "tags": ["quality"],
        },
    )
    assert create.status_code == 201, create.text
    improvement_id = create.json()["id"]

    review = await client.patch(
        f"/api/v1/improvements/{improvement_id}/review",
        json={
            "stage": "beta_test",
            "verdict": "effective",
            "reviewed_by": "owner",
            "review_source": "manual_review",
            "reason": "Separates quality assessment from lifecycle status.",
        },
    )
    assert review.status_code == 200, review.text
    review_body = review.json()
    assert review_body["stage"] == "beta_test"
    assert review_body["verdict"] == "effective"
    assert review_body["status"] == "open"
    assert review_body["last_quality_review_by"] == "owner"
    assert review_body["last_quality_review_source"] == "manual_review"

    artifact = await client.get(f"/api/v1/artifacts/improvement:proj-knowledge:{improvement_id}")
    assert artifact.status_code == 200, artifact.text
    artifact_body = artifact.json()
    assert artifact_body["stage"] == "beta_test"
    assert artifact_body["verdict"] == "effective"
    assert artifact_body["status"] == "open"
