from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.dependencies import get_ollama, get_qdrant
from app.services.crystallization_service import CrystallizationCandidate, apply_crystallization, find_crystallization_candidates
from app.services.project_tree_store import ProjectTreeStore, get_tree_store

PREFIX = "/api/v1"
HEADERS = {"x-api-key": settings.api_key} if settings.api_key else {}


@pytest.fixture(autouse=True)
def _reset_tree_store():
    import app.services.project_tree_store as _pts

    if _pts._store is not None:
        try:
            _pts._store._conn.close()
        except Exception:
            pass
    _pts._store = ProjectTreeStore(Path(":memory:"))
    yield
    if _pts._store is not None:
        try:
            _pts._store._conn.close()
        except Exception:
            pass
        _pts._store = None


async def _create_memory(
    client,
    *,
    content: str,
    scope: str,
    topic_path: str,
    project: str | None = None,
    supports: list[str] | None = None,
) -> str:
    response = await client.post(
        f"{PREFIX}/memories",
        headers=HEADERS,
        json={
            "content": content,
            "agent_id": "crystal-test",
            "memory_type": "fact",
            "category": "canonical" if scope in {"domain", "principle", "meta"} else "general",
            "importance_score": 0.8,
            "project": project,
            "topic_path": topic_path,
            "scope": scope,
            "supports": supports or [],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.asyncio
async def test_find_crystallization_candidates_promotes_upper_scopes(client):
    for idx in range(5):
        await _create_memory(
            client,
            content=f"Shared engineering guidance {idx}",
            scope="domain",
            topic_path=f"engineering/subtopic-{idx}",
            supports=[f"leaf-{idx}"],
        )

    for idx in range(4):
        await _create_memory(
            client,
            content=f"Shared delivery heuristic {idx}",
            scope="principle",
            topic_path=f"delivery/heuristic-{idx}",
            supports=[f"domain-{idx}"],
        )

    qdrant = get_qdrant()
    ollama = get_ollama()
    candidates = await find_crystallization_candidates(
        qdrant._client,
        settings.qdrant_collection_name,
        ollama,
    )

    scopes = {(candidate.target_scope, candidate.topic_path) for candidate in candidates}
    assert ("principle", "engineering") in scopes
    assert ("meta", "delivery") in scopes


@pytest.mark.asyncio
async def test_apply_crystallization_merges_duplicates_and_syncs_tree(client):
    first_leaf = await _create_memory(
        client,
        content="FastAPI caching pattern",
        scope="project",
        topic_path="python/fastapi",
        project="svc-a",
    )
    second_leaf = await _create_memory(
        client,
        content="FastAPI caching pattern reused",
        scope="project",
        topic_path="python/fastapi",
        project="svc-b",
    )

    tree_store = get_tree_store()
    node_id = tree_store.create_node(title="FastAPI", type="project", topic_path="python/fastapi")

    qdrant = get_qdrant()
    ollama = get_ollama()
    candidate_one = CrystallizationCandidate(
        key="cand-1",
        topic_path="python/fastapi",
        target_scope="domain",
        statement="Use shared FastAPI caching conventions for repeatable service behavior.",
        observation="obs",
        why_it_matters="why",
        supports=[first_leaf],
        confidence=0.82,
        project_diversity=2,
        evidence_count=1,
        source_scope="project",
    )
    candidate_two = CrystallizationCandidate(
        key="cand-2",
        topic_path="python/fastapi",
        target_scope="domain",
        statement="Use shared FastAPI caching conventions for repeatable service behavior.",
        observation="obs2",
        why_it_matters="why2",
        supports=[second_leaf],
        confidence=0.9,
        project_diversity=2,
        evidence_count=1,
        source_scope="project",
    )

    canonical_id = await apply_crystallization(candidate_one, qdrant._client, settings.qdrant_collection_name, ollama)
    merged_id = await apply_crystallization(candidate_two, qdrant._client, settings.qdrant_collection_name, ollama)

    assert merged_id == canonical_id

    canonical = await client.get(f"{PREFIX}/memories/{canonical_id}", headers=HEADERS)
    assert canonical.status_code == 200
    payload = canonical.json()
    assert sorted(payload["supports"]) == sorted([first_leaf, second_leaf])

    first_leaf_payload = (await client.get(f"{PREFIX}/memories/{first_leaf}", headers=HEADERS)).json()
    second_leaf_payload = (await client.get(f"{PREFIX}/memories/{second_leaf}", headers=HEADERS)).json()
    assert first_leaf_payload["canonical_id"] == canonical_id
    assert second_leaf_payload["canonical_id"] == canonical_id

    node = tree_store.get_node(node_id)
    assert node["meta_json"]["canonical_memory_id"] == canonical_id
    assert canonical_id in node["meta_json"]["canonical_memory_ids"]


@pytest.mark.asyncio
async def test_hierarchy_endpoints_expose_and_reconcile_canonicals(client):
    canonical_id = await _create_memory(
        client,
        content="Weak canonical",
        scope="domain",
        topic_path="ops/alerts",
        supports=["only-one-support"],
    )

    qdrant = get_qdrant()
    await qdrant._client.set_payload(
        collection_name=settings.qdrant_collection_name,
        payload={"confidence": 0.1, "canonical_status": "active", "suppressed": False},
        points=[canonical_id],
    )

    hierarchy = await client.get(
        f"{PREFIX}/knowledge-hierarchy",
        headers=HEADERS,
        params={"reconcile": True, "include_suppressed": True},
    )
    assert hierarchy.status_code == 200
    body = hierarchy.json()
    assert body["totals"]["domain"] >= 1
    assert body["lifecycle"]["suppressed"] >= 1
    assert any(item["id"] == canonical_id and item["suppressed"] for item in body["by_scope"]["domain"])

    by_scope = await client.get(
        f"{PREFIX}/canonicals/by-scope",
        headers=HEADERS,
        params={"scope": "domain", "include_suppressed": True},
    )
    assert by_scope.status_code == 200
    scope_body = by_scope.json()
    assert any(item["id"] == canonical_id and item["canonical_status"] == "suppressed" for item in scope_body["items"])


@pytest.mark.asyncio
async def test_canonical_governance_endpoints_merge_and_reactivate(client):
    source_id = await _create_memory(
        client,
        content="Canonical A",
        scope="domain",
        topic_path="python/asyncio",
        supports=["leaf-a"],
    )
    target_id = await _create_memory(
        client,
        content="Canonical B",
        scope="domain",
        topic_path="python/asyncio",
        supports=["leaf-b"],
    )

    suppress = await client.patch(
        f"{PREFIX}/canonicals/{source_id}/status",
        headers=HEADERS,
        json={"suppressed": True, "reason": "test"},
    )
    assert suppress.status_code == 200
    assert suppress.json()["canonical_status"] == "suppressed"

    reactivate = await client.patch(
        f"{PREFIX}/canonicals/{source_id}/status",
        headers=HEADERS,
        json={"suppressed": False, "reason": "reactivate"},
    )
    assert reactivate.status_code == 200
    assert reactivate.json()["canonical_status"] == "active"

    merged = await client.post(
        f"{PREFIX}/canonicals/{source_id}/merge",
        headers=HEADERS,
        json={"target_id": target_id},
    )
    assert merged.status_code == 200
    assert merged.json()["target_id"] == target_id

    target = await client.get(f"{PREFIX}/memories/{target_id}", headers=HEADERS)
    assert sorted(target.json()["supports"]) == ["leaf-a", "leaf-b"]
    scope_view = await client.get(
        f"{PREFIX}/canonicals/by-scope",
        headers=HEADERS,
        params={"scope": "domain", "include_suppressed": True},
    )
    source_item = next(item for item in scope_view.json()["items"] if item["id"] == source_id)
    assert source_item["canonical_status"] == "merged"


@pytest.mark.asyncio
async def test_tree_node_exposes_canonical_links(client):
    tree_store = get_tree_store()
    node_id = tree_store.create_node(title="FastAPI", type="project", topic_path="python/fastapi")

    canonical_id = await _create_memory(
        client,
        content="Use shared FastAPI request/response patterns.",
        scope="domain",
        topic_path="python/fastapi",
        supports=["leaf-fastapi"],
    )

    node_resp = await client.get(f"{PREFIX}/tree/{node_id}", headers=HEADERS)
    assert node_resp.status_code == 200
    node_body = node_resp.json()
    assert any(item["id"] == canonical_id for item in node_body["canonicals"])

    links_resp = await client.get(
        f"{PREFIX}/tree/{node_id}/canonicals",
        headers=HEADERS,
        params={"include_suppressed": True},
    )
    assert links_resp.status_code == 200
    links_body = links_resp.json()
    assert links_body["topic_path"] == "python/fastapi"
    assert any(item["id"] == canonical_id for item in links_body["canonicals"])

    md_resp = await client.get(
        f"{PREFIX}/tree/{node_id}/context",
        headers={**HEADERS, "accept": "text/markdown"},
    )
    assert md_resp.status_code == 200
    assert "## Canonical Links" in md_resp.text
    assert "python/fastapi" in md_resp.text
