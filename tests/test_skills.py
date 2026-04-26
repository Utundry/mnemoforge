"""Regression tests for skills module: profile, pack, publish, generate-for-domain."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app import dependencies
from app.routers import skills as skills_router
from app.services.job_queue import get_job_queue
from app.services.memory_store import get_memory_store
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

    @pytest.mark.asyncio
    async def test_scroll_skills_uses_local_domain_filter_without_qdrant_domain_tags_match(self, monkeypatch):
        class FakePoint:
            def __init__(self, pid: str, payload: dict):
                self.id = pid
                self.payload = payload

        observed: dict[str, list[str]] = {}

        class FakeClient:
            async def scroll(self, collection_name, scroll_filter=None, **kwargs):
                observed["must_keys"] = [cond.key for cond in (scroll_filter.must or [])]
                return ([
                    FakePoint("skill-python", {
                        "skill_name": "python-skill",
                        "skill_description": "Handles python tasks",
                        "platform": "claude",
                        "agent_id": "test",
                        "domain_tags": ["python"],
                        "content": "# python-skill\npython content",
                    }),
                    FakePoint("skill-shell", {
                        "skill_name": "shell-skill",
                        "skill_description": "Handles shell tasks",
                        "platform": "claude",
                        "agent_id": "test",
                        "domain_tags": ["shell"],
                        "content": "# shell-skill\nshell content",
                    }),
                ], None)

        class FakeQdrant:
            def __init__(self):
                self._collection = "memories"
                self._client = FakeClient()

        async def passthrough(items):
            return items

        monkeypatch.setattr(skills_router, "_hydrate_content_bulk", passthrough)
        monkeypatch.setattr(skills_router, "_hydrate_counters_bulk", passthrough)

        skills = await skills_router._scroll_skills(FakeQdrant(), domain_filter=["python"], limit=5)
        names = [s["name"] for s in skills]
        assert names == ["python-skill"]
        assert "domain_tags" not in observed.get("must_keys", [])


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

    async def test_pack_create_avoids_domain_tags_filter_panic_path(self, client, monkeypatch):
        published = await self._publish(
            client,
            "fallback-testing-skill",
            ["testing"],
            "Fallback skill for testing",
            "Use this skill when Qdrant scroll fails.",
        )
        assert published.status_code == 200

        real_scroll = dependencies._qdrant_client.scroll

        async def domain_tags_panic_scroll(*args, **kwargs):
            scroll_filter = kwargs.get("scroll_filter")
            must_conditions = list(getattr(scroll_filter, "must", []) or [])
            for condition in must_conditions:
                if getattr(condition, "key", "") == "domain_tags":
                    raise RuntimeError("simulated qdrant panic for domain_tags filter")
            return await real_scroll(*args, **kwargs)

        monkeypatch.setattr(dependencies._qdrant_client, "scroll", domain_tags_panic_scroll)

        r = await client.post("/api/v1/skills/pack/create", json={
            "domains": ["testing"],
            "task_type": "coding",
            "confidence": 0.9,
            "agent_id": "pref-agent",
            "limit": 2,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        names = [s["name"] for s in body["skills"]]
        assert "fallback-testing-skill" in names
        assert body["degraded"] is False


def test_fallback_skill_record_derives_slug_from_heading():
    row = {
        "memory_id": "legacy-1",
        "content": "# Remember Context\n\nKeep project context stable across sessions.",
        "metadata": {
            "skill_name": "",
            "description": "Keep context stable",
            "domain_tags": ["memory"],
            "platform": "claude",
            "agent_id": "shared",
        },
    }
    skill = skills_router._fallback_skill_record_from_store(row)
    assert skill["name"] == "remember-context"
    assert skill["install_path"].endswith("/remember-context/SKILL.md")


def test_fallback_skill_record_replaces_unknown_with_domain_based_name():
    row = {
        "memory_id": "legacy-2",
        "content": "Operational notes without markdown heading",
        "metadata": {
            "skill_name": "unknown",
            "description": "Used for onboarding flows",
            "domain_tags": ["onboarding", "memory"],
            "platform": "claude",
            "agent_id": "shared",
        },
    }
    skill = skills_router._fallback_skill_record_from_store(row)
    assert skill["name"] == "onboarding-skill"
    assert skill["name"] != "unknown"


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

    async def test_publish_persists_rebuild_metadata_in_sqlite(self, client):
        r = await client.post("/api/v1/skills/publish", json={
            "name": "rebuild-skill",
            "content": "# Rebuild Skill\n\nRecover indexes.\n\n## Instructions\nRebuild carefully.",
            "platform": "claude",
            "agent_id": "agent-rebuild",
            "description": "Recovery helper",
            "domain_tags": ["storage", "recovery"],
            "pinned": True,
        })
        assert r.status_code == 200, r.text
        skill_id = r.json()["id"]

        row = await get_memory_store().get(skill_id)
        assert row is not None
        assert row["category"] == "skill"
        meta = row["metadata"]
        assert meta["category"] == "skill"
        assert meta["skill_name"] == "rebuild-skill"
        assert meta["memory_type"] == "context"
        assert meta["agent_id"] == "agent-rebuild"
        assert meta["source"] == "skill-publish:rebuild-skill"
        assert "storage" in meta["domain_tags"]
        assert "rebuild-skill" in meta["tags"]
        assert meta["pinned"] is True


class TestSkillRetag:
    @pytest.mark.asyncio
    async def test_background_retag_registers_handler_when_queue_was_reset(self, client):
        queue = get_job_queue()
        queue._handlers.pop("skills_retag", None)

        resp = await client.post("/api/v1/skills/retag?background=true&limit=1")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "queued"
        assert "skills_retag" in queue._handlers

        job = queue.get_job(data["job_id"])
        assert job is not None
        assert job["job_type"] == "skills_retag"

    @pytest.mark.asyncio
    async def test_run_retag_respects_target_record_ids(self, monkeypatch):
        class FakePoint:
            def __init__(self, pid: str, payload: dict):
                self.id = pid
                self.payload = payload

        class FakeClient:
            def __init__(self):
                self.payload_updates = []

            async def scroll(self, **kwargs):
                return ([
                    FakePoint("skill-a", {"skill_name": "unknown", "domain_tags": [], "content": "Skill A content long enough", "platform": "claude"}),
                    FakePoint("skill-b", {"skill_name": "unknown", "domain_tags": [], "content": "Skill B content long enough", "platform": "claude"}),
                ], None)

            async def retrieve(self, **kwargs):
                ids = kwargs.get("ids") or []
                points = {
                    "skill-a": FakePoint("skill-a", {"skill_name": "unknown", "domain_tags": [], "content": "Skill A content long enough", "platform": "claude"}),
                    "skill-b": FakePoint("skill-b", {"skill_name": "unknown", "domain_tags": [], "content": "Skill B content long enough", "platform": "claude"}),
                }
                return [points[item] for item in ids if item in points]

            async def set_payload(self, **kwargs):
                self.payload_updates.append(kwargs)

            async def update_vectors(self, **kwargs):
                return None

        class FakeQdrant:
            def __init__(self, client):
                self._client = client
                self._collection = "memories"

        class FakeOllama:
            async def embed(self, text: str):
                return [0.1, 0.2, 0.3]

        fake_client = FakeClient()
        fake_qdrant = FakeQdrant(fake_client)
        fake_ollama = FakeOllama()

        async def fake_infer(content: str):
            return {
                "name": f"retagged-{content.split()[1].lower()}",
                "description": "retagged description",
                "domain_tags": ["testing"],
            }

        written_ids: list[str] = []

        async def fake_write_skill_to_store(skill_id: str, *args, **kwargs):
            written_ids.append(skill_id)

        monkeypatch.setattr(skills_router, "_infer_skill_all", fake_infer)
        monkeypatch.setattr(skills_router, "_write_skill_to_store", fake_write_skill_to_store)

        result = await skills_router._run_retag(fake_qdrant, fake_ollama, 10, record_ids=["skill-b"])

        assert result["fixed"] == 1
        assert result["details"][0]["id"] == "skill-b"
        assert written_ids == ["skill-b"]
        assert fake_client.payload_updates[0]["points"] == ["skill-b"]

    @pytest.mark.asyncio
    async def test_run_retag_uses_sqlite_metadata_to_detect_broken_skill_even_when_qdrant_payload_looks_healthy(self, monkeypatch):
        class FakePoint:
            def __init__(self, pid: str, payload: dict):
                self.id = pid
                self.payload = payload

        class FakeClient:
            def __init__(self):
                self.payload_updates = []

            async def retrieve(self, **kwargs):
                return [
                    FakePoint(
                        "skill-stale-sqlite",
                        {
                            "skill_name": "healthy-name",
                            "skill_description": "Healthy description",
                            "domain_tags": ["python"],
                            "content": "# Healthy Name\n\nSkill content long enough",
                            "platform": "claude",
                            "agent_id": "shared",
                            "importance_score": 0.7,
                            "source": "skill-publish:healthy-name",
                        },
                    )
                ]

            async def set_payload(self, **kwargs):
                self.payload_updates.append(kwargs)

            async def update_vectors(self, **kwargs):
                return None

        class FakeQdrant:
            def __init__(self, client):
                self._client = client
                self._collection = "memories"

        class FakeOllama:
            async def embed(self, text: str):
                return [0.1, 0.2, 0.3]

        fake_client = FakeClient()
        fake_qdrant = FakeQdrant(fake_client)
        fake_ollama = FakeOllama()

        async def fake_infer(content: str):
            return {
                "name": "retagged-name",
                "description": "retagged description",
                "domain_tags": ["testing"],
            }

        await get_memory_store().upsert(
            "skill-stale-sqlite",
            "skill",
            "# Healthy Name\n\nSkill content long enough",
            {
                "category": "skill",
                "skill_name": "unknown",
                "description": "",
                "platform": "claude",
                "domain_tags": [],
                "agent_id": "shared",
                "importance_score": 0.7,
                "source": "skill-publish:healthy-name",
                "memory_type": "context",
            },
        )

        monkeypatch.setattr(skills_router, "_infer_skill_all", fake_infer)

        result = await skills_router._run_retag(
            fake_qdrant,
            fake_ollama,
            10,
            record_ids=["skill-stale-sqlite"],
        )

        assert result["fixed"] == 1
        assert result["details"][0]["id"] == "skill-stale-sqlite"
        assert fake_client.payload_updates[0]["points"] == ["skill-stale-sqlite"]

        row = await get_memory_store().get("skill-stale-sqlite")
        assert row is not None
        meta = row["metadata"]
        assert meta["skill_name"] == "retagged-name"
        assert meta["description"] == "retagged description"
        assert meta["domain_tags"] == ["testing"]

    @pytest.mark.asyncio
    async def test_run_retag_recovers_missing_qdrant_skill_from_sqlite(self, monkeypatch):
        class FakePoint:
            def __init__(self, pid: str, payload: dict):
                self.id = pid
                self.payload = payload

        class FakeClient:
            def __init__(self):
                self.points: dict[str, FakePoint] = {}
                self.upserts = []
                self.payload_updates = []

            async def retrieve(self, **kwargs):
                ids = kwargs.get("ids") or []
                return [self.points[item] for item in ids if item in self.points]

            async def upsert(self, **kwargs):
                self.upserts.append(kwargs)
                for point in kwargs.get("points") or []:
                    self.points[str(point.id)] = FakePoint(str(point.id), dict(point.payload or {}))

            async def set_payload(self, **kwargs):
                self.payload_updates.append(kwargs)
                for point_id in kwargs.get("points") or []:
                    payload = dict(kwargs.get("payload") or {})
                    existing = self.points.get(str(point_id))
                    merged = dict(existing.payload or {}) if existing else {}
                    merged.update(payload)
                    self.points[str(point_id)] = FakePoint(str(point_id), merged)

            async def update_vectors(self, **kwargs):
                return None

        class FakeQdrant:
            def __init__(self, client):
                self._client = client
                self._collection = "memories"

        class FakeOllama:
            async def embed(self, text: str):
                return [0.1, 0.2, 0.3]

        fake_client = FakeClient()
        fake_qdrant = FakeQdrant(fake_client)
        fake_ollama = FakeOllama()

        await get_memory_store().upsert(
            "skill-missing-qdrant",
            "skill",
            "# Skill Title\n\nSkill content long enough for recovery",
            {
                "category": "skill",
                "skill_name": "skill-title",
                "description": "Recovered from SQLite",
                "platform": "claude",
                "domain_tags": ["recovery", "testing"],
                "agent_id": "shared",
                "importance_score": 0.7,
                "source": "skill-publish:skill-title",
                "memory_type": "context",
            },
        )

        result = await skills_router._run_retag(
            fake_qdrant,
            fake_ollama,
            10,
            record_ids=["skill-missing-qdrant"],
        )

        assert result["fixed"] == 1
        assert result["recovered_from_sqlite"] == 1
        assert result["total_broken"] == 0
        assert result["details"][0]["id"] == "skill-missing-qdrant"
        assert fake_client.upserts
        recovered = fake_client.points["skill-missing-qdrant"].payload
        assert recovered["skill_name"] == "skill-title"
        assert recovered["domain_tags"] == ["recovery", "testing"]

    @pytest.mark.asyncio
    async def test_run_retag_normalizes_weak_llm_metadata_with_fallbacks(self, monkeypatch):
        class FakePoint:
            def __init__(self, pid: str, payload: dict):
                self.id = pid
                self.payload = payload

        class FakeClient:
            def __init__(self):
                self.payload_updates = []

            async def retrieve(self, **kwargs):
                return [
                    FakePoint(
                        "skill-weak-llm",
                        {
                            "skill_name": "unknown",
                            "skill_description": "",
                            "domain_tags": [],
                            "content": "# Setup Dev\n\nUse this skill to set up the development environment.",
                            "platform": "claude",
                        },
                    )
                ]

            async def set_payload(self, **kwargs):
                self.payload_updates.append(kwargs)

            async def update_vectors(self, **kwargs):
                return None

        class FakeQdrant:
            def __init__(self, client):
                self._client = client
                self._collection = "memories"

        class FakeOllama:
            async def embed(self, text: str):
                return [0.1, 0.2, 0.3]

        async def fake_infer(content: str):
            return {
                "name": "unknown",
                "description": "",
                "domain_tags": [],
            }

        await get_memory_store().upsert(
            "skill-weak-llm",
            "skill",
            "# Setup Dev\n\nUse this skill to set up the development environment.",
            {
                "category": "skill",
                "skill_name": "unknown",
                "description": "",
                "platform": "claude",
                "domain_tags": [],
                "agent_id": "shared",
            },
        )

        monkeypatch.setattr(skills_router, "_infer_skill_all", fake_infer)

        result = await skills_router._run_retag(
            FakeQdrant(FakeClient()),
            FakeOllama(),
            10,
            record_ids=["skill-weak-llm"],
        )

        assert result["fixed"] == 1
        row = await get_memory_store().get("skill-weak-llm")
        assert row is not None
        meta = row["metadata"]
        assert meta["skill_name"] == "setup-dev"
        assert meta["domain_tags"][:2] == ["setup", "development"]
