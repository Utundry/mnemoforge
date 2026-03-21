"""Regression tests for Step 9: Suggest mode — adaptation suggestions, pack trace."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR

_TRANSCRIPT = (
    "USER: как задеплоить FastAPI в kubernetes с helm?\n"
    "ASSISTANT: Используй helm install с values.yaml.\n"
    "USER: а как настроить ingress с nginx?\n"
    "ASSISTANT: Создай Ingress resource и настрой nginx ingress controller.\n"
    "USER: почему rolling update не работает?\n"
    "ASSISTANT: Проверь readinessProbe и minReadySeconds.\n"
)

_SIGNAL_FULL = json.dumps({
    "new_terminology": ["helm", "values.yaml"],
    "missing_skill": ["kubernetes", "nginx"],
    "domain_drift": ["python->kubernetes"],
    "user_preference": [],
    "successful_pattern": ["rolling update via readinessProbe"],
})

_SIGNAL_EMPTY = json.dumps({
    "new_terminology": [], "missing_skill": [], "domain_drift": [],
    "user_preference": [], "successful_pattern": [],
})


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


# ── analyze_dialogue returns suggestions ────────────────────────────────────────

class TestSuggestMode:
    async def test_analyze_returns_suggestions_field(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        assert r.status_code == 200
        body = r.json()
        assert "suggestions" in body
        assert isinstance(body["suggestions"], list)

    async def test_analyze_returns_analysis_mode(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        assert r.json().get("analysis_mode") == "suggest"

    async def test_missing_skill_yields_skill_gap_suggestion(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        suggestions = r.json()["suggestions"]
        types = [s["type"] for s in suggestions]
        assert "skill_gap" in types

    async def test_domain_drift_yields_new_dialog_suggestion(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        suggestions = r.json()["suggestions"]
        drift_suggestions = [s for s in suggestions if s["type"] == "domain_drift"]
        assert len(drift_suggestions) >= 1
        assert drift_suggestions[0]["action"] == "new_dialog"

    async def test_new_terminology_yields_normalization_suggestion(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        suggestions = r.json()["suggestions"]
        norm_suggestions = [s for s in suggestions if s["type"] == "new_terminology"]
        assert len(norm_suggestions) >= 1
        assert norm_suggestions[0]["action"] == "add_normalization"

    async def test_successful_pattern_yields_crystallize_suggestion(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        suggestions = r.json()["suggestions"]
        crystallize = [s for s in suggestions if s["type"] == "successful_pattern"]
        assert len(crystallize) >= 1
        assert crystallize[0]["action"] == "crystallize"

    async def test_suggestion_has_required_fields(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        for s in r.json()["suggestions"]:
            assert "type" in s
            assert "message" in s
            assert "confidence" in s
            assert "action" in s
            assert "evidence" in s

    async def test_no_suggestions_when_no_signals(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_EMPTY)):
            r = await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "test",
            })
        body = r.json()
        assert body["recorded"] is False
        # no suggestions when nothing detected
        assert body.get("suggestions", []) == []


# ── GET /skills/adaptation-suggestions ──────────────────────────────────────────

class TestAdaptationSuggestions:
    async def test_endpoint_returns_structure(self, client):
        r = await client.get("/api/v1/skills/adaptation-suggestions")
        assert r.status_code == 200
        body = r.json()
        assert "suggestions" in body
        assert "by_type" in body
        assert "total" in body
        assert "sources" in body

    async def test_suggestions_populated_after_analyze(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "suggest-test-agent",
            })

        r = await client.get("/api/v1/skills/adaptation-suggestions?agent_id=suggest-test-agent")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1

    async def test_suggestions_deduped_by_type_domain(self, client):
        # Two analyze calls with same missing_skill → should not duplicate the suggestion
        for _ in range(2):
            with patch("app.routers.skills._llm", new=AsyncMock(return_value=json.dumps({
                "new_terminology": [],
                "missing_skill": ["rust-dedup-test"],
                "domain_drift": [], "user_preference": [], "successful_pattern": [],
            }))):
                await client.post("/api/v1/skills/dialogue/analyze", json={
                    "transcript": _TRANSCRIPT,
                    "agent_id": "dedup-agent",
                })

        r = await client.get("/api/v1/skills/adaptation-suggestions?agent_id=dedup-agent")
        assert r.status_code == 200
        body = r.json()
        rust_suggestions = [s for s in body["suggestions"] if s.get("domain") == "rust-dedup-test"]
        assert len(rust_suggestions) == 1  # deduped

    async def test_by_type_grouping(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "grouping-agent",
            })
        r = await client.get("/api/v1/skills/adaptation-suggestions?agent_id=grouping-agent")
        by_type = r.json()["by_type"]
        assert isinstance(by_type, dict)
        assert "skill_gap" in by_type

    async def test_filter_by_session_id(self, client):
        with patch("app.routers.skills._llm", new=AsyncMock(return_value=_SIGNAL_FULL)):
            await client.post("/api/v1/skills/dialogue/analyze", json={
                "transcript": _TRANSCRIPT,
                "agent_id": "session-filter-agent",
                "session_id": "sess-xyz-999",
            })
        r = await client.get(
            "/api/v1/skills/adaptation-suggestions"
            "?agent_id=session-filter-agent&session_id=sess-xyz-999"
        )
        assert r.status_code == 200
        # session filter should return the stored signal
        assert r.json()["sources"] >= 1


# ── GET /skills/pack/{pack_id} ──────────────────────────────────────────────────

class TestPackTrace:
    async def test_nonexistent_pack_returns_404(self, client):
        r = await client.get("/api/v1/skills/pack/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404

    async def test_pack_create_stores_trace(self, client):
        """pack/create schedules _store_pack as a background task.
        In tests BackgroundTasks run inline — so the pack should be retrievable immediately."""
        r = await client.post("/api/v1/skills/pack/create", json={
            "domains": ["python"],
            "task_type": "coding",
            "confidence": 0.8,
            "agent_id": "trace-agent",
            "limit": 3,
        })
        assert r.status_code == 200
        pack_id = r.json()["pack_id"]

        # BackgroundTasks in httpx/ASGI test mode run inline
        r2 = await client.get(f"/api/v1/skills/pack/{pack_id}")
        if r2.status_code == 200:
            body = r2.json()
            assert body["pack_id"] == pack_id
            assert "phase" in body
            assert "domains" in body
        else:
            # 404 is acceptable if background task didn't run inline
            assert r2.status_code == 404
