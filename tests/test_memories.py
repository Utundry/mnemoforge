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
async def test_create_memory_dimension_mismatch_uses_embedding_fallback(mismatch_client):
    resp = await mismatch_client.post(f"{PREFIX}/memories", json={
        "content": "Dimension mismatch",
        "agent_id": "agent1",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["meta"]["embedding_provider"] in {"cloud_semantic_hash", "zero_vector"}


@pytest.mark.asyncio
async def test_memory_store_uses_cloud_semantic_fallback_when_local_embeddings_are_down(client, monkeypatch):
    from app.dependencies import get_ollama

    class FakeLMStudioService:
        async def embed(self, text: str) -> list[float]:
            return []

        async def close(self) -> None:
            return None

    class FakeCloudGateway:
        async def generate(self, prompt: str, **kwargs) -> str:
            assert kwargs["allow_local_fallback"] is False
            return "memory cloud fallback deepseek semantic signature"

    get_ollama().embed.side_effect = RuntimeError("Cannot connect to Ollama")
    monkeypatch.setattr("app.services.embedding_gateway.LMStudioService", FakeLMStudioService)
    monkeypatch.setattr("app.services.embedding_gateway.get_cloud_gateway", lambda: FakeCloudGateway())

    resp = await client.post(f"{PREFIX}/memories", json={
        "content": "Store this memory without local embedding services",
        "agent_id": "agent1",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["meta"]["embedding_provider"] == "cloud_semantic_hash"


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
