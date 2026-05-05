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
    assert "lmstudio" in data
    assert "llm" in data
    assert "llm_providers" in data
    assert "integrity" in data
    assert "data_hygiene" in data
    assert "storage_trust" in data
    assert data["qdrant"]["reachable"] is True
    assert data["ollama"]["reachable"] is True
    assert "reachable" in data["lmstudio"]
    assert "cloud_available" in data["llm"]
    assert "configured_cloud_models" in data["llm"]
    assert data["llm_providers"]["healthy"] is True
    assert "ollama" in data["llm_providers"]["providers"]


@pytest.mark.asyncio
async def test_health_is_ok_when_ollama_down_but_lmstudio_available(client, monkeypatch):
    get_data_integrity_store().clear()
    from app import dependencies
    from app.routers import health as health_router

    dependencies.get_ollama().health.return_value = False

    async def fake_lmstudio_status():
        return {
            "reachable": True,
            "url": "http://localhost:1234/v1",
            "model": "auto",
            "selected_model": "local-lmstudio",
            "models": ["local-lmstudio"],
        }

    monkeypatch.setattr(health_router, "_lmstudio_status", fake_lmstudio_status)
    resp = await client.get(f"{PREFIX}/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["ollama"]["reachable"] is False
    assert data["llm_providers"]["healthy"] is True
    assert "lmstudio" in data["llm_providers"]["usable_providers"]
    assert data["llm_providers"]["available_llms"][0]["provider"] == "lmstudio"


@pytest.mark.asyncio
async def test_health_is_ok_when_only_cloud_llm_available(client, monkeypatch):
    get_data_integrity_store().clear()
    from app import dependencies
    from app.routers import health as health_router

    dependencies.get_ollama().health.return_value = False

    async def fake_lmstudio_status():
        return {
            "reachable": False,
            "url": "http://localhost:1234/v1",
            "model": "auto",
            "selected_model": "",
            "models": [],
        }

    def fake_llm_status(lmstudio_status=None):
        return {
            "local_model": "qwen3:1.7b",
            "local_provider": "auto",
            "local_fallback_order": ["ollama", "lmstudio"],
            "lmstudio": lmstudio_status or {},
            "cloud_available": True,
            "default_cloud_provider": "deepseek:deepseek-chat",
            "configured_cloud_models": ["deepseek-chat"],
            "configured_cloud_model_details": [
                {
                    "model": "deepseek-chat",
                    "provider": "deepseek",
                    "api_style": "openai-chat",
                    "base_url": "https://api.deepseek.com",
                }
            ],
            "gateway": {"local_fallback_enabled": True, "profile_count": 1},
        }

    monkeypatch.setattr(health_router, "_lmstudio_status", fake_lmstudio_status)
    monkeypatch.setattr(health_router, "_llm_status", fake_llm_status)

    resp = await client.get(f"{PREFIX}/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["ollama"]["reachable"] is False
    assert data["llm_providers"]["healthy"] is True
    assert data["llm_providers"]["usable_providers"] == ["cloud"]
    assert data["llm_providers"]["available_llms"] == [
        {
            "id": "deepseek-chat",
            "provider": "deepseek",
            "kind": "cloud_openai_compatible",
            "scope": "cloud",
        }
    ]


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
