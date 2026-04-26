import pytest
from app.services.data_integrity_service import get_data_integrity_store

PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_health(client):
    get_data_integrity_store().clear()
    resp = await client.get(f"{PREFIX}/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "qdrant" in data
    assert "ollama" in data
    assert "llm" in data
    assert "integrity" in data
    assert "data_hygiene" in data
    assert "storage_trust" in data
    assert data["qdrant"]["reachable"] is True
    assert data["ollama"]["reachable"] is True
    assert "cloud_available" in data["llm"]
    assert "configured_cloud_models" in data["llm"]


@pytest.mark.asyncio
async def test_stats(client):
    resp = await client.get(f"{PREFIX}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "points_count" in data
    assert "status" in data


@pytest.mark.asyncio
async def test_health_surfaces_integrity_degradation(client):
    store = get_data_integrity_store()
    store.clear()
    store.upsert_slice(
        slice_id="qdrant.skill_domain_tags_filter",
        subsystem="qdrant",
        status="degraded",
        source="test",
        error="simulated corruption",
    )

    resp = await client.get(f"{PREFIX}/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["integrity"]["status"] == "degraded"
    assert data["storage_trust"]["status"] == "degraded"
    assert "qdrant.skill_domain_tags_filter" in data["integrity"]["degraded_slices"]
