"""Tests for adaptive user workflow guidance (issue ee06864e)."""
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


_EVENTS_FULL = [
    {"type": "context_overload", "context": "transcript > 10k tokens"},
    {"type": "task_switch_detected", "context": "topic changed from python to kubernetes"},
    {"type": "manual_action_required", "context": "need to click UI button"},
]


class TestWorkflowAnalyze:
    async def test_returns_guidance_list(self, client):
        r = await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-test",
            "events": _EVENTS_FULL,
        })
        assert r.status_code == 200
        body = r.json()
        assert "guidance" in body
        assert isinstance(body["guidance"], list)

    async def test_each_event_yields_guidance(self, client):
        r = await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-test2",
            "events": _EVENTS_FULL,
        })
        body = r.json()
        assert body["emitted"] == len(_EVENTS_FULL)
        assert body["throttled"] == 0

    async def test_guidance_has_required_fields(self, client):
        r = await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-fields-test",
            "events": [{"type": "context_overload", "context": "long ctx"}],
        })
        item = r.json()["guidance"][0]
        for field in ("type", "message", "action", "confidence", "context", "throttled"):
            assert field in item

    async def test_unknown_event_type_ignored(self, client):
        r = await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-unknown",
            "events": [{"type": "nonexistent_signal", "context": ""}],
        })
        assert r.status_code == 200
        assert r.json()["total"] == 0

    async def test_throttle_suppresses_repeat(self, client):
        payload = {"agent_id": "wf-throttle", "events": [{"type": "permission_blocker", "context": "denied"}]}
        r1 = await client.post("/api/v1/skills/workflow/analyze", json=payload)
        r2 = await client.post("/api/v1/skills/workflow/analyze", json=payload)
        assert r1.json()["emitted"] == 1
        assert r2.json()["throttled"] == 1
        assert r2.json()["emitted"] == 0

    async def test_context_overload_action_is_new_dialog(self, client):
        r = await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-action",
            "events": [{"type": "context_overload", "context": ""}],
        })
        g = r.json()["guidance"][0]
        assert g["action"] == "new_dialog"

    async def test_manual_action_required_high_confidence(self, client):
        r = await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-conf",
            "events": [{"type": "manual_action_required", "context": "UI step"}],
        })
        g = r.json()["guidance"][0]
        assert g["confidence"] >= 0.85

    async def test_empty_events_returns_zero(self, client):
        r = await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-empty",
            "events": [],
        })
        assert r.json()["total"] == 0


class TestWorkflowGuidanceGet:
    async def test_endpoint_returns_structure(self, client):
        r = await client.get("/api/v1/skills/workflow-guidance")
        assert r.status_code == 200
        body = r.json()
        assert "guidance" in body
        assert "total" in body
        assert "by_type" in body

    async def test_guidance_stored_after_analyze(self, client):
        await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-store-test",
            "events": [{"type": "new_dialog_recommended", "context": "stale ctx"}],
        })
        r = await client.get("/api/v1/skills/workflow-guidance?agent_id=wf-store-test")
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    async def test_by_type_grouping(self, client):
        await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-group",
            "events": [
                {"type": "context_overload", "context": ""},
                {"type": "permission_blocker", "context": ""},
            ],
        })
        r = await client.get("/api/v1/skills/workflow-guidance?agent_id=wf-group")
        by_type = r.json()["by_type"]
        assert "context_overload" in by_type
        assert "permission_blocker" in by_type

    async def test_agent_filter_isolates_results(self, client):
        for agent in ("wf-agent-a", "wf-agent-b"):
            await client.post("/api/v1/skills/workflow/analyze", json={
                "agent_id": agent,
                "events": [{"type": "task_switch_detected", "context": "switch"}],
            })
        r = await client.get("/api/v1/skills/workflow-guidance?agent_id=wf-agent-a")
        agents = [item["agent_id"] for item in r.json()["guidance"]]
        assert all(a == "wf-agent-a" for a in agents)


class TestWorkflowGuidanceRate:
    async def test_rate_useful(self, client):
        await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-rate-test",
            "events": [{"type": "workflow_optimization_opportunity", "context": "use batching"}],
        })
        items = (await client.get("/api/v1/skills/workflow-guidance?agent_id=wf-rate-test")).json()["guidance"]
        assert len(items) >= 1
        gid = items[0]["id"]

        r = await client.post(f"/api/v1/skills/workflow/guidance/{gid}/rate?useful=true")
        assert r.status_code == 200
        assert r.json()["useful_votes"] >= 1

    async def test_rate_not_useful(self, client):
        await client.post("/api/v1/skills/workflow/analyze", json={
            "agent_id": "wf-rate-no",
            "events": [{"type": "context_overload", "context": "big ctx"}],
        })
        items = (await client.get("/api/v1/skills/workflow-guidance?agent_id=wf-rate-no")).json()["guidance"]
        gid = items[0]["id"]

        r = await client.post(f"/api/v1/skills/workflow/guidance/{gid}/rate?useful=false")
        assert r.status_code == 200
        assert r.json()["not_useful_votes"] >= 1

    async def test_rate_invalid_id_returns_400(self, client):
        r = await client.post("/api/v1/skills/workflow/guidance/not-a-uuid/rate?useful=true")
        assert r.status_code == 400

    async def test_rate_nonexistent_returns_404(self, client):
        r = await client.post(
            "/api/v1/skills/workflow/guidance/00000000-0000-0000-0000-000000000000/rate?useful=true"
        )
        assert r.status_code == 404
