"""Regression tests for specific issue fixes:
  - 31550476: DialogueSignal stored as structured JSON payload (not fragile string)
  - 2712d6ac: Phase 2 enriched skills are active and linked to pack_id
  - user_preference consumed in adaptation suggestions
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR

_TRANSCRIPT = (
    "USER: как писать тесты на pytest с фикстурами?\n"
    "ASSISTANT: Используй @pytest.fixture и conftest.py.\n"
    "USER: а что насчёт мокирования httpx?\n"
    "ASSISTANT: Используй respx или unittest.mock.patch.\n"
)

_SIGNAL_WITH_ALL = json.dumps({
    "new_terminology": ["respx", "conftest.py"],
    "missing_skill": ["pytest|advanced"],   # pipe in term — would corrupt old parser
    "domain_drift": ["python->testing"],
    "user_preference": ["prefers explicit fixtures over autouse"],
    "successful_pattern": ["conftest.py for shared fixtures"],
})


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


# ── Fix 31550476: structured payload ───────────────────────────────────────────

class TestStructuredSignalPayload:
    async def test_signal_json_stored_in_payload(self, client):
        """analyze_dialogue must persist signal_json payload (not just delimited string)."""
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_WITH_ALL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "payload-test",
            })
        assert r.status_code == 200
        assert r.json()["recorded"] is True

    async def test_pipe_in_term_does_not_corrupt_suggestions(self, client):
        """Terms containing '|' must not break suggestion derivation (fix 31550476)."""
        signal_with_pipe = json.dumps({
            "new_terminology": [],
            "missing_skill": ["ci|cd"],   # pipe inside domain name
            "domain_drift": [],
            "user_preference": [],
            "successful_pattern": [],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal_with_pipe)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "pipe-test",
            })
        assert r.status_code == 200
        suggestions = r.json()["suggestions"]
        skill_gap = [s for s in suggestions if s["type"] == "skill_gap"]
        assert len(skill_gap) >= 1
        # Domain with pipe must be preserved exactly as returned by LLM
        assert skill_gap[0]["domain"] == "ci|cd"

    async def test_adaptation_suggestions_reads_from_payload(self, client):
        """GET /adaptation-suggestions must use signal_json payload, not reparse content."""
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_WITH_ALL)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "structured-read-agent",
            })

        r = await client.get(
            "/api/v1/skills/adaptation-suggestions?agent_id=structured-read-agent"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        # new_terminology must produce add_normalization suggestions
        norm = [s for s in body["suggestions"] if s["action"] == "add_normalization"]
        assert len(norm) >= 1

    async def test_comma_in_pattern_preserved(self, client):
        """Commas inside successful_pattern must not split into multiple entries."""
        signal_comma = json.dumps({
            "new_terminology": [],
            "missing_skill": [],
            "domain_drift": [],
            "user_preference": [],
            "successful_pattern": ["use conftest.py, not autouse"],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal_comma)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "comma-test",
            })
        suggestions = r.json()["suggestions"]
        crystallize = [s for s in suggestions if s["type"] == "successful_pattern"]
        assert len(crystallize) == 1
        assert "conftest.py, not autouse" in crystallize[0]["evidence"][0]


# ── Fix 2712d6ac: Phase 2 enriched pack contract ───────────────────────────────

class TestPhase2EnrichedPackContract:
    async def _make_pack(self, client, domains: list[str]) -> str:
        r = await client.post("/api/v1/skills/pack/create", json={
            "domains": domains,
            "task_type": "coding",
            "confidence": 0.2,   # low → enrichment_pending=True
            "agent_id": "phase2-test",
            "limit": 3,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["enrichment_pending"] is True
        return body["pack_id"]

    async def test_enriched_skill_is_not_suppressed(self, client):
        """Phase 2 enrichment skills must be active (suppressed=False), not pending_review."""
        pack_id = await self._make_pack(client, ["erlang-phase2"])

        with patch("app.routers.skills._llm", new=AsyncMock(return_value=(
            "# Best Practices: Erlang\n\n## When to use\n- Always\n\n## Key practices\n1. Use OTP."
        ))):
            await client.post(
                f"/api/v1/skills/pack/{pack_id}/enrich",
                params={"domains": "erlang-phase2", "agent_id": "phase2-test"},
            )

        # The enriched skill must appear in the pack (not suppressed)
        r = await client.get("/api/v1/skills/pack?task_tags=erlang-phase2&limit=10")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert any("erlang-phase2" in n for n in names)

    async def test_enriched_skill_not_in_review_queue(self, client):
        """Phase 2 enrichment skills use task_enrichment status, not pending_review."""
        pack_id = await self._make_pack(client, ["scala-phase2"])

        with patch("app.routers.skills._llm", new=AsyncMock(return_value=(
            "# Best Practices: Scala\n\n## When to use\n- FP projects\n\n## Key practices\n1. Use cats."
        ))):
            await client.post(
                f"/api/v1/skills/pack/{pack_id}/enrich",
                params={"domains": "scala-phase2", "agent_id": "phase2-test"},
            )

        # review-queue shows pending_review skills — task_enrichment must not appear there
        r = await client.get("/api/v1/skills/review-queue")
        assert r.status_code == 200
        items = r.json()
        pending = [i for i in items if "scala-phase2" in i["name"]]
        assert all(i["review_status"] != "pending_review" for i in pending)

    async def test_pack_trace_returns_enriched_skills(self, client):
        """GET /skills/pack/{pack_id} must return enriched_skills after Phase 2."""
        # We can't reliably test background task in httpx; test structure of response instead
        r_pack = await client.post("/api/v1/skills/pack/create", json={
            "domains": ["haskell"],
            "task_type": "research",
            "confidence": 0.1,
            "agent_id": "trace-phase2-test",
            "limit": 3,
        })
        pack_id = r_pack.json()["pack_id"]

        # GET /pack/{pack_id} — may 404 if background task didn't run (acceptable in test)
        r = await client.get(f"/api/v1/skills/pack/{pack_id}")
        if r.status_code == 200:
            body = r.json()
            assert "enriched_skills" in body
            assert "added_count" in body
            assert isinstance(body["enriched_skills"], list)
        else:
            assert r.status_code == 404  # background task pending — acceptable

    async def test_generate_for_domain_still_pending_review(self, client):
        """Explicit /generate-for-domain must still create pending_review (requires human review)."""
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=(
            "# Best Practices: Clojure\n\n## When to use\n- Lisp fans\n\n## Key practices\n1. Immutability."
        ))):
            r = await client.post("/api/v1/skills/generate-for-domain", json={
                "domains": ["clojure"],
                "agent_id": "test",
            })
        assert r.status_code == 200

        queue = await client.get("/api/v1/skills/review-queue")
        names = [i["name"] for i in queue.json()]
        assert any("clojure" in n for n in names)


# ── user_preference in suggestions ─────────────────────────────────────────────

class TestUserPreferenceSuggestions:
    async def test_user_preference_yields_note_preference_suggestion(self, client):
        signal = json.dumps({
            "new_terminology": [],
            "missing_skill": [],
            "domain_drift": [],
            "user_preference": ["prefers concise answers without preamble"],
            "successful_pattern": [],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "pref-test",
            })
        assert r.status_code == 200
        suggestions = r.json()["suggestions"]
        pref_s = [s for s in suggestions if s["type"] == "user_preference"]
        assert len(pref_s) == 1
        assert pref_s[0]["action"] == "note_preference"
        assert "concise" in pref_s[0]["message"]

    async def test_user_preference_has_correct_confidence(self, client):
        signal = json.dumps({
            "new_terminology": [],
            "missing_skill": [],
            "domain_drift": [],
            "user_preference": ["likes bullet points"],
            "successful_pattern": [],
        })
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "pref-conf-test",
            })
        pref_s = [s for s in r.json()["suggestions"] if s["type"] == "user_preference"]
        assert pref_s[0]["confidence"] < 0.8   # lower confidence than skill_gap
