"""Regression tests for normalization module: add/list/normalize flow."""
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


class TestNormalization:
    async def test_normalize_passthrough_when_no_glossary(self, client):
        r = await client.post("/api/v1/normalization/normalize", json={
            "text": "deploy to production",
            "agent_id": "test-agent",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["original"] == "deploy to production"
        assert body["was_changed"] is False
        assert body["applied"] == []

    async def test_add_term_returns_record(self, client):
        r = await client.post("/api/v1/normalization/terms", json={
            "term": "sm",
            "expansion": "supermemory",
            "agent_id": "test-agent",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["term"] == "sm"
        assert body["expansion"] == "supermemory"
        assert "id" in body

    async def test_list_terms_for_agent(self, client):
        await client.post("/api/v1/normalization/terms", json={
            "term": "kb",
            "expansion": "knowledge base",
            "agent_id": "list-agent",
        })
        r = await client.get("/api/v1/normalization/terms?agent_id=list-agent")
        assert r.status_code == 200
        terms = r.json()
        assert any(t["term"] == "kb" for t in terms)

    async def test_normalize_applies_glossary_term(self, client):
        await client.post("/api/v1/normalization/terms", json={
            "term": "db",
            "expansion": "database",
            "agent_id": "norm-agent",
        })
        r = await client.post("/api/v1/normalization/normalize", json={
            "text": "connect to db",
            "agent_id": "norm-agent",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["was_changed"] is True
        assert "database" in body["normalized"]
        assert len(body["applied"]) > 0

    async def test_delete_term(self, client):
        add = await client.post("/api/v1/normalization/terms", json={
            "term": "tmp",
            "expansion": "temporary",
            "agent_id": "del-agent",
        })
        assert add.status_code == 201
        body = add.json()
        # id may be top-level or nested under memory_id
        term_id = body.get("id") or body.get("memory_id")
        assert term_id is not None

        r = await client.delete(f"/api/v1/normalization/terms/{term_id}?agent_id=del-agent")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    async def test_terms_are_agent_scoped(self, client):
        await client.post("/api/v1/normalization/terms", json={
            "term": "private-term",
            "expansion": "private expansion",
            "agent_id": "agent-A",
        })
        # agent-B should not see agent-A's terms
        r = await client.get("/api/v1/normalization/terms?agent_id=agent-B")
        assert r.status_code == 200
        terms = r.json()
        assert not any(t["term"] == "private-term" for t in terms)
