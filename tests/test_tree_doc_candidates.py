from __future__ import annotations

from pathlib import Path

import pytest

from app.services.project_tree_doc import regenerate_node_doc
from app.services.project_tree_store import ProjectTreeStore, get_tree_store


@pytest.fixture(autouse=True)
def _reset_tree_store():
    import app.services.project_tree_store as _pts

    if _pts._store is not None:
        try:
            _pts._store.close()
        except Exception:
            pass
    _pts._store = ProjectTreeStore(Path(":memory:"))
    yield
    if _pts._store is not None:
        try:
            _pts._store.close()
        except Exception:
            pass
        _pts._store = None


@pytest.mark.asyncio
async def test_locked_tree_doc_regeneration_stages_candidate(monkeypatch):
    store = get_tree_store()
    node_id = store.create_node(title="Node", type="project", status="active", topic_path="alpha/node")
    store.update_node(
        node_id,
        doc="Effective doc",
        doc_generated_at=100.0,
        meta_json={"doc_locked": True},
    )

    async def fake_generate_doc(node, qdrant=None, ollama=None):
        return "Candidate doc"

    monkeypatch.setattr("app.services.project_tree_doc.generate_doc", fake_generate_doc)
    await regenerate_node_doc(node_id, store, force=False)

    node = store.get_node(node_id)
    assert node["doc"] == "Effective doc"
    assert node["doc_candidate"] == "Candidate doc"
    assert node["doc_generated_at"] == 100.0
    assert node["doc_candidate_generated_at"] is not None


@pytest.mark.asyncio
async def test_tree_doc_candidate_apply_and_discard(client):
    store = get_tree_store()
    node_id = store.create_node(title="Node", type="project", status="active", topic_path="alpha/node")
    store.update_node(
        node_id,
        doc="Effective doc",
        doc_generated_at=100.0,
        doc_candidate="Candidate doc",
        doc_candidate_generated_at=200.0,
        meta_json={"doc_locked": True},
    )

    applied = await client.post(
        f"/api/v1/tree/{node_id}/doc/apply-candidate",
        json={"reviewed_by": "owner", "review_source": "dashboard_review", "reason": "Approved in dashboard"},
    )
    assert applied.status_code == 200
    node = store.get_node(node_id)
    assert node["doc"] == "Candidate doc"
    assert node["doc_candidate"] == ""
    assert node["doc_generated_at"] == 200.0
    assert node["doc_candidate_generated_at"] is None
    assert node["meta_json"]["doc_locked"] is False
    assert node["meta_json"]["doc_last_review_action"] == "apply_candidate"
    assert node["meta_json"]["doc_last_reviewed_by"] == "owner"
    assert node["meta_json"]["doc_last_review_source"] == "dashboard_review"
    assert node["meta_json"]["doc_last_review_reason"] == "Approved in dashboard"

    store.update_node(
        node_id,
        doc="Effective doc v2",
        doc_generated_at=300.0,
        doc_candidate="Discard me",
        doc_candidate_generated_at=400.0,
    )
    discarded = await client.post(
        f"/api/v1/tree/{node_id}/doc/discard-candidate",
        json={"reviewed_by": "owner", "review_source": "dashboard_review", "reason": "Discard candidate"},
    )
    assert discarded.status_code == 200
    node = store.get_node(node_id)
    assert node["doc"] == "Effective doc v2"
    assert node["doc_candidate"] == ""
    assert node["doc_candidate_generated_at"] is None
    assert node["meta_json"]["doc_last_review_action"] == "discard_candidate"
    assert node["meta_json"]["doc_last_reviewed_by"] == "owner"
    assert node["meta_json"]["doc_last_review_source"] == "dashboard_review"
    assert node["meta_json"]["doc_last_review_reason"] == "Discard candidate"
