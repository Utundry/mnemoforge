"""Regression test: conditional reflex for auto-saving reports to supermemory."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


@pytest.mark.asyncio
async def test_report_memories_build_auto_save_reflex(client):
    agent_id = "reflex-test"
    for i in range(6):
        r = await client.post("/api/v1/memories", json={
            "content": f"Report #{i}",
            "agent_id": agent_id,
            "memory_type": "task",
            "category": "qa",
            "importance_score": 0.6,
            "source": "implementation",
            "tags": ["done"],
        })
        assert r.status_code == 201, r.text

    patterns = await client.get(
        f"/api/v1/skills/behavior/patterns?agent_id={agent_id}&suggest_only=true"
    )
    assert patterns.status_code == 200, patterns.text
    body = patterns.json()
    assert body["automatable"] >= 1
    p = next((x for x in body["patterns"] if x["action_type"] == "auto_save_result"), None)
    assert p is not None
    assert p["suggest_automation"] is True
    assert p["context_signature"] == "category:qa"

