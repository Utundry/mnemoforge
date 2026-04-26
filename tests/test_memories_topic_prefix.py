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


async def _store_memory(client, *, content: str, topic_path: str, agent_id: str) -> str:
    resp = await client.post(
        "/api/v1/memories",
        json={
            "content": content,
            "agent_id": agent_id,
            "memory_type": "fact",
            "category": "general",
            "topic_path": topic_path,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_memory_search_topic_prefix_filters_candidate_pool(client):
    agent_id = "topic-prefix-search-agent"
    infra_id = await _store_memory(
        client,
        content="shared-topic-probe memory for infra reverse proxy setup",
        topic_path="infra/nginx/reverse-proxy",
        agent_id=agent_id,
    )
    python_id = await _store_memory(
        client,
        content="shared-topic-probe memory for python fastapi error handling",
        topic_path="python/fastapi/errors",
        agent_id=agent_id,
    )

    resp = await client.post(
        "/api/v1/memories/search",
        json={
            "query": "shared-topic-probe",
            "agent_id": agent_id,
            "topic_prefix": "infra",
            "limit": 10,
        },
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    ids = {item["memory"]["id"] for item in rows}
    assert infra_id in ids
    assert python_id not in ids
    assert all((item["memory"].get("topic_path") or "").startswith("infra") for item in rows)


@pytest.mark.asyncio
async def test_memory_context_topic_prefix_filters_rendered_context(client):
    agent_id = "topic-prefix-context-agent"
    await _store_memory(
        client,
        content="shared-topic-probe infra hint for load balancer tuning",
        topic_path="infra/network/load-balancer",
        agent_id=agent_id,
    )
    await _store_memory(
        client,
        content="shared-topic-probe python hint for fastapi timeout handling",
        topic_path="python/fastapi/timeouts",
        agent_id=agent_id,
    )

    resp = await client.post(
        "/api/v1/memories/context",
        json={
            "query": "shared-topic-probe",
            "agent_id": agent_id,
            "topic_prefix": "python",
            "limit": 10,
            "max_tokens": 400,
            "format": "text",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source_count"] >= 1
    assert "python hint for fastapi timeout handling" in data["context"]
    assert "infra hint for load balancer tuning" not in data["context"]
