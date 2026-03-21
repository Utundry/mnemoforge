"""Regression tests for entities module: CRUD and relations."""
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


ENTITY_PAYLOAD = {
    "name": "Alice",
    "entity_type": "user",
    "agent_id": "test-agent",
    "description": "Senior engineer who likes Python",
    "attributes": {"role": "engineer", "language": "python"},
    "tags": ["vip"],
    "importance_score": 0.9,
}


class TestEntityCRUD:
    async def test_create_returns_record(self, client):
        r = await client.post("/api/v1/entities", json=ENTITY_PAYLOAD)
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Alice"
        assert body["entity_type"] == "user"
        assert body["agent_id"] == "test-agent"
        assert "id" in body

    async def test_get_by_id(self, client):
        create = await client.post("/api/v1/entities", json=ENTITY_PAYLOAD)
        entity_id = create.json()["id"]

        r = await client.get(f"/api/v1/entities/{entity_id}")
        assert r.status_code == 200
        assert r.json()["id"] == entity_id
        assert r.json()["name"] == "Alice"

    async def test_get_nonexistent_returns_404(self, client):
        r = await client.get("/api/v1/entities/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404

    async def test_update_description(self, client):
        create = await client.post("/api/v1/entities", json=ENTITY_PAYLOAD)
        entity_id = create.json()["id"]

        r = await client.put(f"/api/v1/entities/{entity_id}", json={
            "description": "Updated: now a tech lead",
            "importance_score": 0.95,
        })
        assert r.status_code == 200
        assert r.json()["importance_score"] == 0.95

    async def test_delete_entity(self, client):
        create = await client.post("/api/v1/entities", json=ENTITY_PAYLOAD)
        entity_id = create.json()["id"]

        r = await client.delete(f"/api/v1/entities/{entity_id}")
        assert r.status_code == 204

        # Should be gone
        r2 = await client.get(f"/api/v1/entities/{entity_id}")
        assert r2.status_code == 404

    async def test_list_requires_agent_id(self, client):
        r = await client.get("/api/v1/entities")
        assert r.status_code == 422

    async def test_create_minimal_entity(self, client):
        r = await client.post("/api/v1/entities", json={
            "name": "Bob",
            "entity_type": "agent",
            "agent_id": "test-agent",
        })
        assert r.status_code == 201
        assert r.json()["name"] == "Bob"


class TestEntityRelations:
    async def _create_entity(self, client, name: str, etype: str = "user") -> str:
        r = await client.post("/api/v1/entities", json={
            "name": name,
            "entity_type": etype,
            "agent_id": "test-agent",
        })
        return r.json()["id"]

    async def test_create_relation(self, client):
        alice_id = await self._create_entity(client, "Alice")
        project_id = await self._create_entity(client, "ProjectAlpha", "project")

        r = await client.post("/api/v1/entities/relations", json={
            "from_entity_id": alice_id,
            "to_entity_id": project_id,
            "relation_type": "works_on",
            "agent_id": "test-agent",
            "description": "Alice is leading this project",
            "strength": 0.9,
        })
        assert r.status_code == 201
        body = r.json()
        assert body["from_entity_id"] == alice_id
        assert body["to_entity_id"] == project_id
        assert body["relation_type"] == "works_on"

    async def test_list_relations_from(self, client):
        alice_id = await self._create_entity(client, "Alice2")
        project_id = await self._create_entity(client, "ProjectBeta", "project")

        await client.post("/api/v1/entities/relations", json={
            "from_entity_id": alice_id,
            "to_entity_id": project_id,
            "relation_type": "owns",
            "agent_id": "test-agent",
        })

        r = await client.get(f"/api/v1/entities/{alice_id}/relations?direction=from")
        assert r.status_code == 200
        relations = r.json()
        assert any(rel["from_entity_id"] == alice_id for rel in relations)

    async def test_delete_relation(self, client):
        alice_id = await self._create_entity(client, "Alice3")
        project_id = await self._create_entity(client, "ProjectGamma", "project")

        rel = await client.post("/api/v1/entities/relations", json={
            "from_entity_id": alice_id,
            "to_entity_id": project_id,
            "relation_type": "uses",
            "agent_id": "test-agent",
        })
        rel_id = rel.json()["id"]

        r = await client.delete(f"/api/v1/entities/relations/{rel_id}")
        assert r.status_code == 204
