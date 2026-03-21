"""Regression tests for skills module: profile, pack, publish, generate-for-domain."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


# ── /skills/profile ────────────────────────────────────────────────────────────

class TestSkillProfile:
    async def test_detects_memory_domain(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "remember this fact about the database"})
        assert r.status_code == 200
        body = r.json()
        assert "memory" in body["domains"] or "database" in body["domains"]

    async def test_detects_api_domain(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "add new REST API endpoint with http request"})
        assert r.status_code == 200
        assert "api" in r.json()["domains"]

    async def test_detects_task_type_feature(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "implement a new feature for the backend"})
        assert r.status_code == 200
        assert r.json()["task_type"] == "feature"

    async def test_detects_task_type_bug(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "fix the broken error in production"})
        assert r.status_code == 200
        assert r.json()["task_type"] == "bug"

    async def test_returns_confidence(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "write python code for the api endpoint"})
        assert r.status_code == 200
        body = r.json()
        assert 0.0 <= body["confidence"] <= 1.0

    async def test_short_text_returns_other(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "hello"})
        assert r.status_code == 200
        body = r.json()
        assert body["task_type"] == "other"
        assert body["domains"] == []

    async def test_detects_russian_feature_task_type(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "реализуй новую фичу для backend"})
        assert r.status_code == 200
        assert r.json()["task_type"] == "feature"

    async def test_detects_russian_bug_task_type(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": "не работает endpoint, почини"})
        assert r.status_code == 200
        assert r.json()["task_type"] == "bug"

    async def test_empty_text_rejected(self, client):
        r = await client.post("/api/v1/skills/profile", json={"text": ""})
        assert r.status_code == 422


# ── /skills/pack ───────────────────────────────────────────────────────────────

class TestSkillPack:
    async def _publish(self, client, name: str, tags: list[str]):
        return await client.post("/api/v1/skills/publish", json={
            "name": name,
            "content": f"# {name}\n\nThis skill handles {name} tasks.\n\n## Instructions\nDo the thing.",
            "platform": "claude",
            "agent_id": "test",
            "description": f"Test skill for {name}",
            "domain_tags": tags,
            "importance_score": 0.8,
        })

    async def test_empty_pack_when_no_skills(self, client):
        r = await client.get("/api/v1/skills/pack?task_tags=nonexistent-domain-xyz")
        assert r.status_code == 200
        assert r.json() == []

    async def test_returns_matching_skill(self, client):
        await self._publish(client, "test-memory-skill", ["memory", "recall"])
        r = await client.get("/api/v1/skills/pack?task_tags=memory&limit=5")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert "test-memory-skill" in names

    async def test_does_not_return_unrelated_skill(self, client):
        await self._publish(client, "deploy-skill", ["deploy", "cloudflare"])
        r = await client.get("/api/v1/skills/pack?task_tags=memory&limit=5")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert "deploy-skill" not in names

    async def test_respects_limit(self, client):
        for i in range(5):
            await self._publish(client, f"skill-limit-{i}", ["python"])
        r = await client.get("/api/v1/skills/pack?task_tags=python&limit=2")
        assert r.status_code == 200
        assert len(r.json()) <= 2

    async def test_prefers_more_useful_skill_over_raw_importance(self, client):
        low_importance = await self._publish(client, "useful-python-skill", ["python"])
        high_importance = await client.post("/api/v1/skills/publish", json={
            "name": "important-python-skill",
            "content": "# important-python-skill\n\nHandles python tasks.\n\n## Instructions\nUse it.",
            "platform": "claude",
            "agent_id": "test",
            "description": "Important but not yet proven",
            "domain_tags": ["python"],
            "importance_score": 0.95,
        })
        assert low_importance.status_code == 200
        assert high_importance.status_code == 200

        useful_id = low_importance.json()["id"]
        important_id = high_importance.json()["id"]

        outcome = await client.post("/api/v1/skills/outcome", json={
            "pack_id": "ranking-test-pack",
            "skills_helpful": [useful_id],
            "skills_unused": [important_id],
            "missing_domains": [],
            "success": True,
            "agent_id": "test",
        })
        assert outcome.status_code == 200

        r = await client.get("/api/v1/skills/pack?task_tags=python&limit=2")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert names[:2] == ["useful-python-skill", "important-python-skill"]

    async def test_pack_item_has_content(self, client):
        await self._publish(client, "content-skill", ["testing"])
        r = await client.get("/api/v1/skills/pack?task_tags=testing&limit=1")
        assert r.status_code == 200
        items = r.json()
        if items:
            assert items[0]["content"] != ""

    async def test_missing_task_tags_rejected(self, client):
        r = await client.get("/api/v1/skills/pack")
        assert r.status_code == 422


class TestPreferenceAwarePack:
    async def _publish(self, client, name: str, tags: list[str], description: str, content_hint: str):
        return await client.post("/api/v1/skills/publish", json={
            "name": name,
            "content": f"# {name}\n\n{content_hint}\n\n## Instructions\n{content_hint}",
            "platform": "claude",
            "agent_id": "pref-agent",
            "description": description,
            "domain_tags": tags,
            "importance_score": 0.8,
        })

    async def _save_preference(self, client, preference: str):
        signal = json.dumps({
            "new_terminology": [],
            "missing_skill": [],
            "domain_drift": [],
            "user_preference": [preference],
            "successful_pattern": [],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": (
                    "USER: please tailor how you answer.\n"
                    "ASSISTANT: sure, tell me your preference.\n"
                    "USER: I prefer concise answers.\n"
                ),
                "agent_id": "pref-agent",
                "session_id": "pref-session",
            })
        assert r.status_code == 200

    async def test_get_pack_prefers_skills_matching_user_preference(self, client):
        await self._publish(
            client,
            "concise-python-skill",
            ["python"],
            "Concise answers for python tasks",
            "Use concise answers without preamble for python tasks.",
        )
        await self._publish(
            client,
            "verbose-python-skill",
            ["python"],
            "Detailed walkthrough for python tasks",
            "Provide detailed walkthroughs with extensive explanation.",
        )
        await self._save_preference(client, "prefers concise answers without preamble")

        r = await client.get("/api/v1/skills/pack?task_tags=python&limit=2&agent_id=pref-agent")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert names[:2] == ["concise-python-skill", "verbose-python-skill"]

    async def test_pack_create_uses_agent_preferences_in_ranking(self, client):
        await self._publish(
            client,
            "bullet-testing-skill",
            ["testing"],
            "Bullet point guidance for testing",
            "Use bullet points and concise answers for testing tasks.",
        )
        await self._publish(
            client,
            "essay-testing-skill",
            ["testing"],
            "Essay style guidance for testing",
            "Write long-form essays for testing tasks.",
        )
        await self._save_preference(client, "likes bullet points")

        r = await client.post("/api/v1/skills/pack/create", json={
            "domains": ["testing"],
            "task_type": "coding",
            "confidence": 0.9,
            "agent_id": "pref-agent",
            "limit": 2,
        })
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["skills"]]
        assert names[:2] == ["bullet-testing-skill", "essay-testing-skill"]


# ── /skills/publish ────────────────────────────────────────────────────────────

class TestSkillPublish:
    async def test_publish_returns_skill_record(self, client):
        r = await client.post("/api/v1/skills/publish", json={
            "name": "my-skill",
            "content": "# My Skill\n\nDoes something useful.\n\n## Instructions\nStep 1.",
            "platform": "claude",
            "agent_id": "agent1",
            "description": "A useful skill",
            "domain_tags": ["python", "testing"],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "my-skill"
        assert body["platform"] == "claude"
        assert "id" in body

    async def test_published_skill_appears_in_search(self, client):
        await client.post("/api/v1/skills/publish", json={
            "name": "search-test-skill",
            "content": "# Search Test\n\nFor search testing.\n\n## Instructions\nSearch.",
            "platform": "claude",
            "agent_id": "agent1",
            "description": "Search test skill",
            "domain_tags": ["search", "testing"],
        })
        r = await client.get("/api/v1/skills/search?domains=search")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert "search-test-skill" in names
