import pytest

PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_search_returns_results(client):
    # Store two memories
    for content in ["Python is fun", "I love async code"]:
        await client.post(f"{PREFIX}/memories", json={"content": content, "agent_id": "agent1"})

    resp = await client.post(f"{PREFIX}/memories/search", json={
        "query": "programming language",
        "agent_id": "agent1",
        "limit": 5,
    })
    assert resp.status_code == 200
    results = resp.json()
    assert isinstance(results, list)
    assert len(results) >= 1
    # Each result has expected keys
    for r in results:
        assert "memory" in r
        assert "score" in r
        assert "similarity" in r


@pytest.mark.asyncio
async def test_search_agent_isolation(client):
    await client.post(f"{PREFIX}/memories", json={"content": "Agent A secret", "agent_id": "agentA"})
    await client.post(f"{PREFIX}/memories", json={"content": "Agent B data", "agent_id": "agentB"})

    resp = await client.post(f"{PREFIX}/memories/search", json={
        "query": "secret data",
        "agent_id": "agentA",
        "limit": 10,
    })
    assert resp.status_code == 200
    agent_ids = {r["memory"]["agent_id"] for r in resp.json()}
    assert agent_ids <= {"agentA"}


@pytest.mark.asyncio
async def test_batch_create(client):
    resp = await client.post(f"{PREFIX}/memories/batch", json={
        "memories": [
            {"content": "Batch item 1", "agent_id": "agent1"},
            {"content": "Batch item 2", "agent_id": "agent1"},
        ]
    })
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["created_ids"]) == 2
    assert data["failed_count"] == 0


@pytest.mark.asyncio
async def test_batch_create_partial_failure_returns_207(partial_batch_client):
    resp = await partial_batch_client.post(f"{PREFIX}/memories/batch", json={
        "memories": [
            {"content": "Batch item 1", "agent_id": "agent1"},
            {"content": "Batch item 2", "agent_id": "agent1"},
        ]
    })
    assert resp.status_code == 207
    data = resp.json()
    assert len(data["created_ids"]) == 1
    assert data["failed_count"] == 1


@pytest.mark.asyncio
async def test_batch_create_full_failure_returns_500(failed_batch_client):
    resp = await failed_batch_client.post(f"{PREFIX}/memories/batch", json={
        "memories": [
            {"content": "Batch item 1", "agent_id": "agent1"},
            {"content": "Batch item 2", "agent_id": "agent1"},
        ]
    })
    assert resp.status_code == 500
    data = resp.json()
    assert data["created_ids"] == []
    assert data["failed_count"] == 2


@pytest.mark.asyncio
async def test_entities_create_and_list(client):
    create = await client.post(f"{PREFIX}/entities", json={
        "name": "alice",
        "entity_type": "user",
        "agent_id": "entity-agent",
        "description": "Platform administrator",
        "attributes": {"team": "infra", "role": "admin"},
        "tags": ["test"],
        "importance_score": 0.9,
    })
    assert create.status_code == 201
    created = create.json()
    assert created["name"] == "alice"
    assert created["entity_type"] == "user"

    listed = await client.get(f"{PREFIX}/entities", params={"agent_id": "entity-agent"})
    assert listed.status_code == 200
    data = listed.json()
    assert len(data) == 1
    assert data[0]["id"] == created["id"]
    assert data[0]["attributes"] == {"team": "infra", "role": "admin"}
