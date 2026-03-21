from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


async def _publish_skill(client, name: str, tags: list[str]):
    return await client.post("/api/v1/skills/publish", json={
        "name": name,
        "content": f"# {name}\n\nHandles {name} tasks.\n\n## Instructions\nStep 1.",
        "platform": "claude",
        "agent_id": "observability-test",
        "description": f"Skill for {name}",
        "domain_tags": tags,
        "importance_score": 0.8,
    })


class TestAdaptiveObservability:
    async def test_summary_tracks_adaptive_lifecycle_events(self, client):
        before = await client.get("/api/v1/skills/observability-summary")
        assert before.status_code == 200
        before_total = before.json()["total_events"]

        published = await _publish_skill(client, "obs-python-skill", ["python"])
        assert published.status_code == 200
        skill_id = published.json()["id"]

        profile = await client.post("/api/v1/skills/profile", json={"text": "write python code for api"})
        assert profile.status_code == 200

        pack = await client.get("/api/v1/skills/pack?task_tags=python&limit=5")
        assert pack.status_code == 200

        signal = json.dumps({
            "new_terminology": ["respx"],
            "missing_skill": ["pytest"],
            "domain_drift": [],
            "user_preference": ["prefers concise answers"],
            "successful_pattern": [],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)):
            analyzed = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": (
                    "USER: how do I mock httpx in pytest?\n"
                    "ASSISTANT: use patch or respx.\n"
                    "USER: I prefer concise answers.\n"
                ),
                "agent_id": "observability-test",
                "session_id": "obs-session",
            })
        assert analyzed.status_code == 200

        suggestions = await client.get(
            "/api/v1/skills/adaptation-suggestions?agent_id=observability-test&session_id=obs-session"
        )
        assert suggestions.status_code == 200

        outcome = await client.post("/api/v1/skills/outcome", json={
            "pack_id": "obs-pack",
            "skills_helpful": [skill_id],
            "skills_unused": [],
            "missing_domains": ["pytest"],
            "success": True,
            "agent_id": "observability-test",
        })
        assert outcome.status_code == 200

        after = await client.get("/api/v1/skills/observability-summary")
        assert after.status_code == 200
        body = after.json()

        assert body["component"] == "adaptive-skillization"
        assert body["total_events"] >= before_total + 5
        assert body["success_rate"] is not None
        assert "enrichment_usefulness_avg" in body
        assert body["scope"] is None  # no scope filter applied
        for task_type in [
            "task_profile",
            "skill_pack_fast",
            "dialogue_analyze",
            "adaptation_suggestions",
            "skill_outcome_report",
        ]:
            assert task_type in body["task_types"]

    async def test_scope_filter_by_agent_id(self, client):
        """?agent_id= returns scope field and filters events to that agent."""
        signal = json.dumps({
            "new_terminology": [], "missing_skill": [], "domain_drift": [],
            "user_preference": [], "successful_pattern": [],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": "USER: hi\nASSISTANT: hello\nUSER: ok\nASSISTANT: ok",
                "agent_id": "scope-filter-agent",
            })

        r = await client.get("/api/v1/skills/observability-summary?agent_id=scope-filter-agent")
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == {"agent_id": "scope-filter-agent"}

    async def test_latency_percentiles_present(self, client):
        """Each task_type entry must include latency_percentiles with p50/p95/p99."""
        signal = json.dumps({
            "new_terminology": ["k8s"], "missing_skill": [], "domain_drift": [],
            "user_preference": [], "successful_pattern": [],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": "USER: what is k8s?\nASSISTANT: kubernetes\nUSER: ok\nASSISTANT: sure",
                "agent_id": "pct-agent",
            })

        r = await client.get("/api/v1/skills/observability-summary?agent_id=pct-agent")
        assert r.status_code == 200
        task_types = r.json()["task_types"]
        assert "dialogue_analyze" in task_types
        pct = task_types["dialogue_analyze"].get("latency_percentiles")
        assert pct is not None
        assert "p50" in pct
        assert "p95" in pct
        assert "p99" in pct

    async def test_since_hours_filter(self, client):
        """?since_hours= is accepted and returns scope with since_hours."""
        r = await client.get("/api/v1/skills/observability-summary?since_hours=1")
        assert r.status_code == 200
        body = r.json()
        assert body["scope"]["since_hours"] == 1.0
