from __future__ import annotations

import pytest

from app.config import settings
from app.dependencies import get_qdrant


PREFIX = "/api/v1/layout"


@pytest.mark.asyncio
async def test_layout_feedback_records_explicit_review_metadata(client):
    fixed = await client.post(
        f"{PREFIX}/fix",
        json={"text": "ghbdtn", "agent_id": "tester"},
    )
    assert fixed.status_code == 200, fixed.text
    correction_id = fixed.json()["id"]

    feedback = await client.post(
        f"{PREFIX}/feedback",
        json={
            "correction_id": correction_id,
            "confirmed": False,
            "correct_text": "привет",
            "reviewed_by": "owner",
            "review_source": "dashboard_review",
            "reason": "Wrong correction for this context",
        },
    )
    assert feedback.status_code == 200, feedback.text
    body = feedback.json()
    assert body["confirmed"] is False
    assert body["last_feedback_action"] == "reject_layout_fix"
    assert body["last_feedback_by"] == "owner"
    assert body["last_feedback_source"] == "dashboard_review"
    assert body["last_feedback_reason"] == "Wrong correction for this context"

    qdrant = get_qdrant()
    points = await qdrant._client.retrieve(
        collection_name=settings.qdrant_collection_name,
        ids=[correction_id],
        with_payload=True,
        with_vectors=False,
    )
    assert points
    payload = points[0].payload or {}
    assert payload["confirmed"] is False
    assert payload["user_correction"] == "привет"
    assert payload["last_feedback_action"] == "reject_layout_fix"
    assert payload["last_feedback_by"] == "owner"
    assert payload["last_feedback_source"] == "dashboard_review"
    assert payload["last_feedback_reason"] == "Wrong correction for this context"
    assert payload["last_feedback_at"]
    assert payload["importance_score"] == 0.1


@pytest.mark.asyncio
async def test_layout_fix_keeps_short_latin_typo_unchanged(client):
    resp = await client.post(
        f"{PREFIX}/fix",
        json={"text": "teh", "agent_id": "tester"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["was_fixed"] is False
    assert body["direction"] == "none"
    assert body["corrected"] == "teh"
    assert body["method"] == "rule"


@pytest.mark.asyncio
async def test_layout_fix_still_handles_keyboard_layout_word(client):
    resp = await client.post(
        f"{PREFIX}/fix",
        json={"text": "ghbdtn", "agent_id": "tester"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["was_fixed"] is True
    assert body["direction"] == "en->ru"
    assert body["corrected"] != "ghbdtn"
