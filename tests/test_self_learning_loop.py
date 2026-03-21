"""Tests for self-learning loop (issue 71831a04):
- POST /normalization/feedback  — feedback-driven glossary update
- GET  /skills/gaps             — domain gap aggregation
- GET  /skills/analytics        — pack quality metrics
"""
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


# ── POST /normalization/feedback ─────────────────────────────────────────────


class TestNormalizationFeedback:
    async def test_positive_feedback_recorded(self, client):
        r = await client.post("/api/v1/normalization/feedback", json={
            "agent_id": "fb-agent",
            "term": "k8s",
            "was_helpful": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["feedback_recorded"] is True
        assert body["term_updated"] is False
        assert body["new_expansion"] is None

    async def test_negative_feedback_without_correction(self, client):
        r = await client.post("/api/v1/normalization/feedback", json={
            "agent_id": "fb-agent",
            "term": "ci",
            "was_helpful": False,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["feedback_recorded"] is True
        assert body["term_updated"] is False

    async def test_negative_feedback_with_correction_updates_term(self, client):
        # First add an existing term
        await client.post("/api/v1/normalization/terms", json={
            "agent_id": "fb-correct",
            "term": "rq",
            "expansion": "review queue",
        })
        # Provide correction
        r = await client.post("/api/v1/normalization/feedback", json={
            "agent_id": "fb-correct",
            "term": "rq",
            "was_helpful": False,
            "corrected_expansion": "request queue",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["feedback_recorded"] is True
        assert body["term_updated"] is True
        assert body["new_expansion"] == "request queue"

    async def test_correction_updates_glossary(self, client):
        """After correction, normalize should use the new expansion."""
        await client.post("/api/v1/normalization/terms", json={
            "agent_id": "fb-norm",
            "term": "mq",
            "expansion": "memory queue",
        })
        await client.post("/api/v1/normalization/feedback", json={
            "agent_id": "fb-norm",
            "term": "mq",
            "was_helpful": False,
            "corrected_expansion": "message queue",
        })
        r = await client.post("/api/v1/normalization/normalize", json={
            "text": "push to mq",
            "agent_id": "fb-norm",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["was_changed"] is True
        assert "message queue" in body["normalized"]

    async def test_required_fields(self, client):
        r = await client.post("/api/v1/normalization/feedback", json={
            "agent_id": "fb-agent",
            # missing 'term' and 'was_helpful'
        })
        assert r.status_code == 422

    async def test_response_has_required_fields(self, client):
        r = await client.post("/api/v1/normalization/feedback", json={
            "agent_id": "fb-fields",
            "term": "svc",
            "was_helpful": True,
        })
        body = r.json()
        for field in ("feedback_recorded", "term_updated", "new_expansion"):
            assert field in body


# ── GET /skills/gaps ─────────────────────────────────────────────────────────


async def _record_outcome(client, agent_id: str, missing_domains: list[str], success: bool = True):
    r = await client.post("/api/v1/skills/outcome", json={
        "pack_id": "test-pack",
        "agent_id": agent_id,
        "skills_helpful": [],
        "skills_unused": [],
        "missing_domains": missing_domains,
        "success": success,
    })
    assert r.status_code == 200


class TestSkillGaps:
    async def test_gaps_empty_when_no_outcomes(self, client):
        r = await client.get("/api/v1/skills/gaps?agent_id=no-outcomes-agent")
        assert r.status_code == 200
        body = r.json()
        assert body["gaps"] == []
        assert body["total_outcomes"] == 0

    async def test_gaps_returns_missing_domains(self, client):
        await _record_outcome(client, "gaps-agent", ["security", "deployment"])
        await _record_outcome(client, "gaps-agent", ["security", "auth"])

        r = await client.get("/api/v1/skills/gaps?agent_id=gaps-agent")
        assert r.status_code == 200
        domains = {g["domain"] for g in r.json()["gaps"]}
        assert "security" in domains

    async def test_gaps_suggested_when_count_reaches_threshold(self, client):
        for _ in range(3):
            await _record_outcome(client, "gaps-suggest", ["testing"])

        r = await client.get("/api/v1/skills/gaps?agent_id=gaps-suggest&min_count=2")
        gaps = r.json()["gaps"]
        testing_gap = next((g for g in gaps if g["domain"] == "testing"), None)
        assert testing_gap is not None
        assert testing_gap["suggested"] is True
        assert testing_gap["count"] == 3

    async def test_gaps_not_suggested_below_threshold(self, client):
        await _record_outcome(client, "gaps-low", ["rare-domain"])

        r = await client.get("/api/v1/skills/gaps?agent_id=gaps-low&min_count=3")
        gaps = r.json()["gaps"]
        domain_gap = next((g for g in gaps if g["domain"] == "rare-domain"), None)
        if domain_gap:
            assert domain_gap["suggested"] is False

    async def test_gaps_sorted_by_count_descending(self, client):
        await _record_outcome(client, "gaps-sort", ["a-domain"])
        for _ in range(3):
            await _record_outcome(client, "gaps-sort", ["b-domain"])

        r = await client.get("/api/v1/skills/gaps?agent_id=gaps-sort")
        gaps = r.json()["gaps"]
        if len(gaps) >= 2:
            assert gaps[0]["count"] >= gaps[1]["count"]

    async def test_gaps_response_structure(self, client):
        r = await client.get("/api/v1/skills/gaps?agent_id=gaps-struct")
        body = r.json()
        assert "gaps" in body
        assert "total_outcomes" in body
        assert "agent_id" in body
        assert "min_count" in body


# ── GET /skills/analytics ─────────────────────────────────────────────────────


class TestSkillAnalytics:
    async def test_analytics_empty_agent(self, client):
        r = await client.get("/api/v1/skills/analytics?agent_id=analytics-empty")
        assert r.status_code == 200
        body = r.json()
        assert body["total_outcomes"] == 0
        assert body["success_rate"] is None

    async def test_analytics_success_rate(self, client):
        await _record_outcome(client, "analytics-rate", [], success=True)
        await _record_outcome(client, "analytics-rate", [], success=True)
        await _record_outcome(client, "analytics-rate", [], success=False)

        r = await client.get("/api/v1/skills/analytics?agent_id=analytics-rate")
        body = r.json()
        assert body["total_outcomes"] == 3
        assert body["success_count"] == 2
        assert abs(body["success_rate"] - 0.667) < 0.01

    async def test_analytics_top_helpful_skills(self, client):
        r = await client.post("/api/v1/skills/outcome", json={
            "pack_id": "p1",
            "agent_id": "analytics-helpful",
            "skills_helpful": ["skill-a", "skill-b"],
            "skills_unused": [],
            "missing_domains": [],
            "success": True,
        })
        assert r.status_code == 200

        r = await client.get("/api/v1/skills/analytics?agent_id=analytics-helpful")
        body = r.json()
        helpful_ids = [h["skill_id"] for h in body["top_helpful_skills"]]
        assert "skill-a" in helpful_ids
        assert "skill-b" in helpful_ids

    async def test_analytics_top_missing_domains(self, client):
        for _ in range(2):
            await _record_outcome(client, "analytics-missing", ["infra", "security"])

        r = await client.get("/api/v1/skills/analytics?agent_id=analytics-missing")
        body = r.json()
        domain_names = [d["domain"] for d in body["top_missing_domains"]]
        assert "infra" in domain_names
        assert "security" in domain_names

    async def test_analytics_response_structure(self, client):
        r = await client.get("/api/v1/skills/analytics?agent_id=analytics-struct")
        body = r.json()
        for field in (
            "agent_id", "total_outcomes", "success_count", "success_rate",
            "top_helpful_skills", "top_unused_skills", "top_missing_domains",
        ):
            assert field in body

    async def test_analytics_unused_tracked_separately(self, client):
        await client.post("/api/v1/skills/outcome", json={
            "pack_id": "p2",
            "agent_id": "analytics-unused",
            "skills_helpful": [],
            "skills_unused": ["skill-unused-x"],
            "missing_domains": [],
            "success": True,
        })
        r = await client.get("/api/v1/skills/analytics?agent_id=analytics-unused")
        body = r.json()
        unused_ids = [u["skill_id"] for u in body["top_unused_skills"]]
        assert "skill-unused-x" in unused_ids
