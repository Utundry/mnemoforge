"""Integration test: vector-based semantic dedup for Learning Ledger candidates."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_learning_candidates_vector_dedup_across_context(client):
    # Two candidates: different context_signature, very different wording.
    # With mocked embeddings (same vector), vector dedup must collapse them.
    r1 = await client.post("/api/v1/learning/candidates", json={
        "artifact_type": "hint",
        "action_type": "run_tests",
        "content": "Запускай pytest перед коммитом.",
        "agent_id": "test",
        "project": "proj-a",
        "task_type": "ci",
        "transport": "api",
        "tags": ["t1"],
    })
    assert r1.status_code == 201
    b1 = r1.json()
    assert b1["created"] is True
    cid = b1["id"]

    r2 = await client.post("/api/v1/learning/candidates", json={
        "artifact_type": "hint",
        "action_type": "run_tests",
        "content": "Перед пушем обязательно прогоняй проверки качества.",
        "agent_id": "test",
        "project": "proj-b",
        "task_type": "release",
        "transport": "api",
        "tags": ["t2"],
    })
    assert r2.status_code == 201
    b2 = r2.json()
    assert b2["created"] is False
    assert b2["id"] == cid

    # Evidence increments on the single candidate
    arts = await client.get("/api/v1/learning/artifacts?scope=candidate&status=pending_review&limit=500")
    assert arts.status_code == 200
    items = arts.json()["artifacts"]
    row = next((a for a in items if a.get("id") == cid), None)
    assert row is not None
    assert row["evidence_count"] == 2

