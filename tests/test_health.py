import pytest

PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get(f"{PREFIX}/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "qdrant" in data
    assert "ollama" in data
    assert data["qdrant"]["reachable"] is True
    assert data["ollama"]["reachable"] is True


@pytest.mark.asyncio
async def test_stats(client):
    resp = await client.get(f"{PREFIX}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "points_count" in data
    assert "status" in data
