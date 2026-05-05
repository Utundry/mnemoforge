"""Regression tests for importance decay gating by project activity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.config import settings
from app.services import qdrant_service
from tests.conftest import _build_client, MOCK_VECTOR


@pytest_asyncio.fixture
async def client_and_qdrant():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c, qdrant_client
    await qdrant_client.close()


async def _create_project_memory(client, *, project: str, importance: float = 0.7) -> str:
    r = await client.post("/api/v1/memories", json={
        "content": f"decay gate test ({project})",
        "agent_id": "gov-agent",
        "memory_type": "fact",
        "category": "general",
        "importance_score": importance,
        "project": project,
    })
    assert r.status_code == 201
    return r.json()["id"]


@pytest.mark.asyncio
async def test_decay_skips_sleeping_project(client_and_qdrant):
    client, _ = client_and_qdrant
    qdrant_service._project_activity.clear()

    await _create_project_memory(client, project="sleepy")

    r = await client.post("/api/v1/governance/decay", json={
        "idle_days": 1,
        "decay_step": 0.05,
        "floor": 0.05,
        "dry_run": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["affected"] == 0
    assert body["skipped_recent"] >= 1  # frozen


@pytest.mark.asyncio
async def test_decay_applies_when_project_active(client_and_qdrant):
    client, qdrant_client = client_and_qdrant
    qdrant_service._project_activity.clear()

    mem_id = await _create_project_memory(client, project="active", importance=0.7)

    # Mark project active via /context (updates activity gate + last_access_ts)
    ctx = await client.post("/api/v1/memories/context", json={
        "query": "decay gate test",
        "agent_id": "gov-agent",
        "limit": 5,
        "max_tokens": 300,
        "format": "text",
        "context_project": "active",
    })
    assert ctx.status_code == 200

    # Make the memory eligible for decay by backdating last_decay_ts.
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    await qdrant_client.set_payload(
        collection_name=settings.qdrant_collection_name,
        payload={"last_decay_ts": old},
        points=[mem_id],
    )

    r = await client.post("/api/v1/governance/decay", json={
        "idle_days": 1,
        "decay_step": 0.05,
        "floor": 0.05,
        "dry_run": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["affected"] >= 1
    assert any(c["id"] == mem_id for c in body["candidates"])
