"""Tests for behavioral adaptation layer (issue a05fbce2)."""
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


async def _record(client, agent_id: str, action_type: str, accepted: bool, n: int = 1):
    for _ in range(n):
        r = await client.post("/api/v1/skills/behavior/record", json={
            "agent_id": agent_id,
            "action_type": action_type,
            "accepted": accepted,
        })
        assert r.status_code == 200
    return r.json()


class TestBehaviorRecord:
    async def test_returns_required_fields(self, client):
        r = await client.post("/api/v1/skills/behavior/record", json={
            "agent_id": "ba-test",
            "action_type": "save_to_memory",
            "accepted": True,
        })
        assert r.status_code == 200
        body = r.json()
        for field in ("action_type", "accepted", "confidence", "recent_confidence",
                      "suggest_automation", "high_risk"):
            assert field in body

    async def test_confidence_increases_with_accepts(self, client):
        r1 = await _record(client, "ba-conf1", "save_note", accepted=True)
        r2 = await _record(client, "ba-conf1", "save_note", accepted=True)
        assert r2["confidence"] >= r1["confidence"]

    async def test_suggest_automation_after_threshold(self, client):
        """After 5+ accepts with high confidence, suggest_automation becomes True."""
        body = None
        for _ in range(7):
            r = await client.post("/api/v1/skills/behavior/record", json={
                "agent_id": "ba-suggest",
                "action_type": "auto_save_result",
                "accepted": True,
            })
            body = r.json()
        assert body["suggest_automation"] is True

    async def test_suggest_false_below_threshold(self, client):
        """Only 2 accepts — not enough for automation suggestion."""
        body = await _record(client, "ba-low", "save_result", accepted=True, n=2)
        assert body["suggest_automation"] is False

    async def test_rejection_reduces_confidence(self, client):
        await _record(client, "ba-rej", "save_note", accepted=True, n=5)
        r_before = await _record(client, "ba-rej", "save_note", accepted=True)
        conf_before = r_before["confidence"]
        r_after = await _record(client, "ba-rej", "save_note", accepted=False)
        assert r_after["confidence"] < conf_before

    async def test_high_risk_action_never_suggested(self, client):
        """High-risk actions must never get suggest_automation=True regardless of history."""
        for action in ("delete_file", "force_push", "rm_rf", "deploy_production"):
            body = await _record(client, "ba-highrisk", action, accepted=True, n=10)
            assert body["suggest_automation"] is False
            assert body["high_risk"] is True

    async def test_normal_action_not_high_risk(self, client):
        body = await _record(client, "ba-normal", "save_to_memory", accepted=True)
        assert body["high_risk"] is False


class TestBehaviorPatterns:
    async def test_patterns_endpoint_returns_structure(self, client):
        r = await client.get("/api/v1/skills/behavior/patterns?agent_id=ba-patterns-test")
        assert r.status_code == 200
        body = r.json()
        assert "patterns" in body
        assert "total" in body
        assert "automatable" in body

    async def test_patterns_populated_after_records(self, client):
        await _record(client, "ba-pop", "tag_memory", accepted=True, n=3)
        r = await client.get("/api/v1/skills/behavior/patterns?agent_id=ba-pop")
        assert r.json()["total"] >= 1

    async def test_suggest_only_filter(self, client):
        """suggest_only=true returns only patterns ready for automation."""
        await _record(client, "ba-filter", "low_risk_action", accepted=True, n=7)
        await _record(client, "ba-filter", "another_action", accepted=True, n=1)

        all_r = await client.get("/api/v1/skills/behavior/patterns?agent_id=ba-filter")
        suggest_r = await client.get(
            "/api/v1/skills/behavior/patterns?agent_id=ba-filter&suggest_only=true"
        )
        assert suggest_r.json()["total"] <= all_r.json()["total"]
        for p in suggest_r.json()["patterns"]:
            assert p["suggest_automation"] is True

    async def test_decay_detected_after_rejections(self, client):
        """After many accepts then several rejects, decaying flag should appear."""
        await _record(client, "ba-decay", "confirm_action", accepted=True, n=8)
        await _record(client, "ba-decay", "confirm_action", accepted=False, n=4)
        r = await client.get("/api/v1/skills/behavior/patterns?agent_id=ba-decay")
        pattern = next(
            (p for p in r.json()["patterns"] if p["action_type"] == "confirm_action"), None
        )
        assert pattern is not None
        # recent_confidence should be lower than overall confidence
        assert pattern["recent_confidence"] < pattern["confidence"]

    async def test_automatable_count(self, client):
        await _record(client, "ba-count", "quick_save", accepted=True, n=7)
        r = await client.get("/api/v1/skills/behavior/patterns?agent_id=ba-count")
        body = r.json()
        assert body["automatable"] == sum(
            1 for p in body["patterns"] if p["suggest_automation"]
        )


class TestBehaviorReset:
    async def test_reset_removes_pattern(self, client):
        await _record(client, "ba-reset", "deletable_action", accepted=True, n=3)
        r_before = await client.get("/api/v1/skills/behavior/patterns?agent_id=ba-reset")
        assert r_before.json()["total"] >= 1

        r = await client.post(
            "/api/v1/skills/behavior/patterns/deletable_action/reset?agent_id=ba-reset"
        )
        assert r.status_code == 200
        assert r.json()["reset"] is True

        r_after = await client.get("/api/v1/skills/behavior/patterns?agent_id=ba-reset")
        names = [p["action_type"] for p in r_after.json()["patterns"]]
        assert "deletable_action" not in names

    async def test_reset_nonexistent_returns_404(self, client):
        r = await client.post(
            "/api/v1/skills/behavior/patterns/nonexistent_action/reset?agent_id=ba-reset-404"
        )
        assert r.status_code == 404
