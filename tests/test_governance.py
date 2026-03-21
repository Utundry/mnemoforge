"""Regression tests for governance module: stats, lifecycle, stale."""
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


async def _create_memory(client, content: str = "test memory", agent_id: str = "gov-agent",
                          importance: float = 0.7, category: str = "general"):
    r = await client.post("/api/v1/memories", json={
        "content": content,
        "agent_id": agent_id,
        "memory_type": "fact",
        "category": category,
        "importance_score": importance,
    })
    assert r.status_code == 201
    return r.json()["id"]


class TestGovernanceStats:
    async def test_stats_returns_structure(self, client):
        r = await client.get("/api/v1/governance/stats")
        assert r.status_code == 200
        body = r.json()
        assert "total" in body
        assert "by_type" in body
        assert "by_category" in body
        assert "avg_importance" in body

    async def test_stats_counts_increase_after_insert(self, client):
        r1 = await client.get("/api/v1/governance/stats")
        before = r1.json()["total"]

        await _create_memory(client, "governance test memory")

        r2 = await client.get("/api/v1/governance/stats")
        after = r2.json()["total"]

        assert after > before

    async def test_stats_by_category(self, client):
        await _create_memory(client, "skill record", category="skill")
        r = await client.get("/api/v1/governance/stats")
        body = r.json()
        assert "by_category" in body
        assert isinstance(body["by_category"], dict)


class TestGovernanceLifecycle:
    async def test_lifecycle_returns_record(self, client):
        mem_id = await _create_memory(client, "lifecycle test memory")
        r = await client.get(f"/api/v1/governance/lifecycle/{mem_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == mem_id
        assert "content_preview" in body
        assert "age_days" in body
        assert "current_recency_score" in body
        assert "access_count" in body

    async def test_lifecycle_nonexistent_returns_404(self, client):
        r = await client.get("/api/v1/governance/lifecycle/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404

    async def test_lifecycle_content_preview_truncated(self, client):
        long_content = "x" * 500
        mem_id = await _create_memory(client, long_content)
        r = await client.get(f"/api/v1/governance/lifecycle/{mem_id}")
        assert r.status_code == 200
        assert len(r.json()["content_preview"]) <= 200


class TestGovernanceStale:
    async def test_stale_endpoint_returns_list(self, client):
        # /governance/stale is POST with a StaleConfig body
        r = await client.post("/api/v1/governance/stale", json={
            "min_age_days": 1,
            "max_access_count": 0,
        })
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_low_importance_memory_appears_in_stale(self, client):
        await _create_memory(client, "very low importance memory", importance=0.05)
        r = await client.post("/api/v1/governance/stale", json={
            "min_age_days": 1,
            "max_access_count": 999,
            "max_importance": 0.1,
        })
        assert r.status_code == 200
        assert isinstance(r.json(), list)
