"""Regression tests for Review Queue (step 8) and auto-improvements (step 7)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR

_TRANSCRIPT = (
    "USER: как настроить nginx reverse proxy для FastAPI?\n"
    "ASSISTANT: Добавь server block с proxy_pass http://127.0.0.1:8000.\n"
    "USER: а как настроить SSL с certbot?\n"
    "ASSISTANT: Запусти certbot --nginx -d yourdomain.com.\n"
)

_SIGNAL_WITH_MISSING = json.dumps({
    "new_terminology": ["certbot"],
    "missing_skill": ["nginx", "ssl"],
    "domain_drift": [],
    "user_preference": [],
    "successful_pattern": [],
})


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


async def _auto_generate_skill(client, domains: list[str]) -> dict:
    """Trigger generate-for-domain to create a pending_review skill."""
    with patch("app.routers.skills._llm", new=AsyncMock(return_value=(
        "# Best Practices: Test\n\n## When to use\n- Always\n\n## Key practices\n1. Do good things."
    ))):
        r = await client.post("/api/v1/skills/generate-for-domain", json={
            "domains": domains,
            "agent_id": "test",
        })
    assert r.status_code == 200
    return r.json()


# ── Review Queue ───────────────────────────────────────────────────────────────

class TestReviewQueue:
    async def test_review_queue_returns_list(self, client):
        r = await client.get("/api/v1/skills/review-queue")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_auto_generated_skill_appears_in_queue(self, client):
        await _auto_generate_skill(client, ["nginx-test-domain"])
        r = await client.get("/api/v1/skills/review-queue")
        assert r.status_code == 200
        names = [item["name"] for item in r.json()]
        assert any("nginx-test-domain" in n for n in names)

    async def test_auto_generated_skill_has_pending_status(self, client):
        await _auto_generate_skill(client, ["pending-domain-xyz"])
        r = await client.get("/api/v1/skills/review-queue")
        assert r.status_code == 200
        items = r.json()
        pending = [i for i in items if "pending-domain-xyz" in i["name"]]
        assert len(pending) >= 1
        assert pending[0]["review_status"] == "pending_review"
        assert pending[0]["auto_generated"] is True

    async def test_auto_generated_skill_excluded_from_pack(self, client):
        """Auto-generated skills are suppressed until approved — must not appear in packs."""
        await _auto_generate_skill(client, ["excluded-domain-abc"])
        r = await client.get("/api/v1/skills/pack?task_tags=excluded-domain-abc&limit=10")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert not any("excluded-domain-abc" in n for n in names)

    async def test_manually_published_skill_not_in_queue(self, client):
        """Manually published skills bypass review — must not appear in review queue."""
        await client.post("/api/v1/skills/publish", json={
            "name": "manual-skill",
            "content": "# Manual\n\nManual skill.\n\n## Instructions\nStep 1.",
            "platform": "claude",
            "agent_id": "test",
            "description": "Manual skill",
            "domain_tags": ["manual"],
        })
        r = await client.get("/api/v1/skills/review-queue")
        assert r.status_code == 200
        names = [i["name"] for i in r.json()]
        assert "manual-skill" not in names

    async def test_review_queue_item_has_content(self, client):
        await _auto_generate_skill(client, ["content-check-domain"])
        r = await client.get("/api/v1/skills/review-queue")
        assert r.status_code == 200
        items = [i for i in r.json() if "content-check-domain" in i["name"]]
        if items:
            assert items[0]["content"] != ""
            assert "domain_tags" in items[0]


# ── Approve ────────────────────────────────────────────────────────────────────

class TestApproveReject:
    async def test_approve_makes_skill_active(self, client):
        await _auto_generate_skill(client, ["approve-domain"])
        queue = await client.get("/api/v1/skills/review-queue")
        items = [i for i in queue.json() if "approve-domain" in i["name"]]
        assert len(items) >= 1
        skill_id = items[0]["id"]

        r = await client.post(f"/api/v1/skills/review/{skill_id}/approve")
        assert r.status_code == 200
        body = r.json()
        assert body["review_status"] == "approved"
        assert body["active"] is True
        assert body["last_review_action"] == "approve_skill"
        assert body["last_reviewed_by"] == "user"
        assert body["last_review_source"] == "inline_user_approval"

    async def test_approved_skill_appears_in_pack(self, client):
        await _auto_generate_skill(client, ["approved-pack-domain"])
        queue = await client.get("/api/v1/skills/review-queue")
        items = [i for i in queue.json() if "approved-pack-domain" in i["name"]]
        skill_id = items[0]["id"]

        await client.post(f"/api/v1/skills/review/{skill_id}/approve")

        r = await client.get("/api/v1/skills/pack?task_tags=approved-pack-domain&limit=10")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert any("approved-pack-domain" in n for n in names)

    async def test_approve_removes_from_queue(self, client):
        await _auto_generate_skill(client, ["remove-from-queue-domain"])
        queue_before = await client.get("/api/v1/skills/review-queue")
        items = [i for i in queue_before.json() if "remove-from-queue-domain" in i["name"]]
        skill_id = items[0]["id"]

        await client.post(f"/api/v1/skills/review/{skill_id}/approve")

        queue_after = await client.get("/api/v1/skills/review-queue")
        names_after = [i["name"] for i in queue_after.json()]
        assert not any("remove-from-queue-domain" in n for n in names_after)

    async def test_reject_keeps_skill_suppressed(self, client):
        await _auto_generate_skill(client, ["reject-domain"])
        queue = await client.get("/api/v1/skills/review-queue")
        items = [i for i in queue.json() if "reject-domain" in i["name"]]
        skill_id = items[0]["id"]

        r = await client.post(
            f"/api/v1/skills/review/{skill_id}/reject?reason=not+useful",
            json={"reviewed_by": "owner", "review_source": "dashboard_review"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["review_status"] == "rejected"
        assert body["active"] is False
        assert body["last_review_action"] == "reject_skill"
        assert body["last_reviewed_by"] == "owner"
        assert body["last_review_source"] == "dashboard_review"
        assert body["last_review_reason"] == "not useful"

    async def test_rejected_skill_not_in_pack(self, client):
        await _auto_generate_skill(client, ["rejected-pack-domain"])
        queue = await client.get("/api/v1/skills/review-queue")
        items = [i for i in queue.json() if "rejected-pack-domain" in i["name"]]
        skill_id = items[0]["id"]
        await client.post(f"/api/v1/skills/review/{skill_id}/reject")

        r = await client.get("/api/v1/skills/pack?task_tags=rejected-pack-domain&limit=10")
        assert r.status_code == 200
        assert r.json() == []

    async def test_approve_invalid_id_returns_400(self, client):
        r = await client.post("/api/v1/skills/review/not-a-uuid/approve")
        assert r.status_code == 400

    async def test_approve_nonexistent_returns_404(self, client):
        r = await client.post("/api/v1/skills/review/00000000-0000-0000-0000-000000000000/approve")
        assert r.status_code == 404


# ── Step 7: auto-improvements ──────────────────────────────────────────────────

class TestAutoImprovements:
    async def test_dialogue_analyze_creates_improvement_for_missing_skill(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_WITH_MISSING)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })

        r = await client.get("/api/v1/artifacts?project=supermemory&type=improvement&artifact_status=open&limit=50")
        assert r.status_code == 200
        data = r.json()
        items = data.get("items", [])
        titles = [i["title"] for i in items]
        assert any("nginx" in t.lower() for t in titles)

    async def test_improvement_has_correct_tags(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_WITH_MISSING)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })

        r = await client.get("/api/v1/artifacts?project=supermemory&type=improvement&artifact_status=open&limit=50")
        assert r.status_code == 200
        data = r.json()
        items = data.get("items", [])
        skill_gap_items = [i for i in items if "skill-gap" in i.get("tags", [])]
        assert len(skill_gap_items) >= 1

    async def test_no_improvements_when_no_missing_skills(self, client):
        empty_signal = json.dumps({
            "new_terminology": ["helm"],
            "missing_skill": [],
            "domain_drift": [],
            "user_preference": [],
            "successful_pattern": [],
        })
        r_before = await client.get("/api/v1/artifacts?project=supermemory&type=improvement&artifact_status=open&limit=50")
        count_before = len(r_before.json().get("items", []))

        with patch("app.routers.skills._llm", new=AsyncMock(return_value=empty_signal)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })

        r_after = await client.get("/api/v1/artifacts?project=supermemory&type=improvement&artifact_status=open&limit=50")
        assert len(r_after.json().get("items", [])) == count_before
