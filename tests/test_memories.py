import pytest

PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_create_memory(client):
    resp = await client.post(f"{PREFIX}/memories", json={
        "content": "User prefers concise answers",
        "agent_id": "agent1",
        "memory_type": "preference",
        "importance_score": 0.8,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["content"] == "User prefers concise answers"
    assert data["agent_id"] == "agent1"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_memory(client):
    create = await client.post(f"{PREFIX}/memories", json={
        "content": "Python is great",
        "agent_id": "agent1",
    })
    mid = create.json()["id"]

    get = await client.get(f"{PREFIX}/memories/{mid}")
    assert get.status_code == 200
    assert get.json()["id"] == mid


@pytest.mark.asyncio
async def test_get_not_found(client):
    resp = await client.get(f"{PREFIX}/memories/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_memory(client):
    create = await client.post(f"{PREFIX}/memories", json={
        "content": "Old content",
        "agent_id": "agent1",
    })
    mid = create.json()["id"]

    update = await client.put(f"{PREFIX}/memories/{mid}", json={
        "importance_score": 0.9,
        "category": "updated",
    })
    assert update.status_code == 200
    assert update.json()["importance_score"] == 0.9
    assert update.json()["category"] == "updated"


@pytest.mark.asyncio
async def test_delete_memory(client):
    create = await client.post(f"{PREFIX}/memories", json={
        "content": "To be deleted",
        "agent_id": "agent1",
    })
    mid = create.json()["id"]

    delete = await client.delete(f"{PREFIX}/memories/{mid}")
    assert delete.status_code == 204

    get = await client.get(f"{PREFIX}/memories/{mid}")
    assert get.status_code == 404


@pytest.mark.asyncio
async def test_create_memory_dimension_mismatch_returns_422(mismatch_client):
    resp = await mismatch_client.post(f"{PREFIX}/memories", json={
        "content": "Dimension mismatch",
        "agent_id": "agent1",
    })
    assert resp.status_code == 422
    assert "Vector dimension mismatch" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_normalization_add_term_and_normalize(client):
    add = await client.post(f"{PREFIX}/normalization/terms", json={
        "term": "supramemory",
        "expansion": "semantic memory system",
        "agent_id": "norm-agent",
        "global_scope": False,
    })
    assert add.status_code == 201

    normalize = await client.post(f"{PREFIX}/normalization/normalize", json={
        "text": "please inspect supramemory server bugs",
        "agent_id": "norm-agent",
    })
    assert normalize.status_code == 200
    data = normalize.json()
    assert data["was_changed"] is True
    assert data["normalized"] == "please inspect semantic memory system server bugs"
    assert data["applied"] == [{"term": "supramemory", "expansion": "semantic memory system"}]
