from __future__ import annotations

import json

import pytest

from app.dependencies import get_qdrant
from app.services.learning_store import get_learning_store


@pytest.mark.asyncio
async def test_confirm_drafts_records_confirmation_metadata(client, monkeypatch):
    async def fake_llm(prompt: str) -> str:
        return json.dumps(
            [
                {
                    "content": "Remember this draft memory.",
                    "memory_type": "fact",
                    "importance": 0.8,
                    "tags": ["draft-test"],
                }
            ]
        )

    monkeypatch.setattr("app.routers.auto_memory._llm", fake_llm)

    preview = await client.post(
        "/api/v1/auto/extract/preview",
        json={
            "text": "Remember this draft memory.",
            "agent_id": "tester",
            "store_drafts": True,
        },
    )
    assert preview.status_code == 200
    draft_id = preview.json()["candidates"][0]["id"]
    assert draft_id

    confirm = await client.post(
        "/api/v1/auto/draft/confirm",
        json={
            "draft_ids": [draft_id],
            "confirmed_by": "owner",
            "confirmation_source": "dashboard_review",
            "reason": "Approved from dashboard",
        },
    )
    assert confirm.status_code == 200
    assert confirm.json()["confirmed"] == 1

    points = await get_qdrant()._client.retrieve(
        collection_name=get_qdrant()._collection,
        ids=[draft_id],
        with_payload=True,
        with_vectors=False,
    )
    payload = points[0].payload or {}
    assert payload["category"] == "general"
    assert payload["confirmed_by"] == "owner"
    assert payload["confirmation_source"] == "dashboard_review"
    assert payload["confirmation_reason"] == "Approved from dashboard"
    assert payload["confirmed_at"]


@pytest.mark.asyncio
async def test_confirm_drafts_rejects_non_draft_memory(client):
    create = await client.post(
        "/api/v1/memories",
        json={
            "content": "Active memory",
            "agent_id": "tester",
        },
    )
    assert create.status_code == 201
    memory_id = create.json()["id"]

    confirm = await client.post(
        "/api/v1/auto/draft/confirm",
        json={"draft_ids": [memory_id]},
    )
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["confirmed"] == 0
    assert body["failed"] == 1


@pytest.mark.asyncio
async def test_discard_drafts_ignores_non_draft_memory(client):
    create = await client.post(
        "/api/v1/memories",
        json={
            "content": "Active memory",
            "agent_id": "tester",
        },
    )
    assert create.status_code == 201
    memory_id = create.json()["id"]

    discard = await client.post(
        "/api/v1/auto/draft/discard",
        json={"draft_ids": [memory_id]},
    )
    assert discard.status_code == 200
    assert discard.json()["discarded"] == 0


@pytest.mark.asyncio
async def test_discard_drafts_emits_review_audit_event(client, monkeypatch):
    async def fake_llm(prompt: str) -> str:
        return json.dumps(
            [
                {
                    "content": "Discard this draft memory.",
                    "memory_type": "fact",
                    "importance": 0.8,
                    "tags": ["draft-test"],
                }
            ]
        )

    monkeypatch.setattr("app.routers.auto_memory._llm", fake_llm)

    preview = await client.post(
        "/api/v1/auto/extract/preview",
        json={
            "text": "Discard this draft memory.",
            "agent_id": "tester",
            "store_drafts": True,
        },
    )
    assert preview.status_code == 200
    draft_id = preview.json()["candidates"][0]["id"]
    assert draft_id

    discard = await client.post(
        "/api/v1/auto/draft/discard",
        json={
            "draft_ids": [draft_id],
            "discarded_by": "owner",
            "discard_source": "dashboard_review",
            "reason": "Not useful",
        },
    )
    assert discard.status_code == 200
    assert discard.json()["discarded"] == 1

    events = await get_learning_store().list_events(limit=20)
    event = next(
        e for e in events
        if e["event_type"] == "user_feedback"
        and json.loads(e["payload_json"]).get("action") == "discard_draft"
        and json.loads(e["payload_json"]).get("draft_id") == draft_id
    )
    payload = json.loads(event["payload_json"])
    assert event["agent_id"] == "owner"
    assert payload["discard_source"] == "dashboard_review"
    assert payload["reason"] == "Not useful"
