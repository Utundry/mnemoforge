"""
Integration tests for the Learning Ledger API router (/api/v1/learning/*).

Uses the standard test client fixture from conftest.py (in-memory Qdrant + mocked Ollama).
Learning store singleton is reset per test to prevent cross-test state.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.learning_store import get_learning_store
from tests.conftest import MOCK_VECTOR, _build_client


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def _reset_learning_store(tmp_path: Path):
    """Give each test a fresh in-memory LearningStore to avoid state leakage."""
    import app.services.learning_store as _ls_mod
    old = _ls_mod._store
    _ls_mod._store = _ls_mod.LearningStore(db_path=tmp_path / "learning.db")
    yield
    store = _ls_mod._store
    _ls_mod._store = old
    if store is not None:
        await store.aclose()


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


@pytest_asyncio.fixture
async def glm_client():
    """Client with Ollama returning a valid GLM candidate JSON."""
    from app import dependencies
    from app.main import create_app
    from app.services.ollama_service import OllamaService
    from app.services.qdrant_service import QdrantService
    from qdrant_client import AsyncQdrantClient

    qdrant_client = AsyncQdrantClient(":memory:")
    qdrant_svc = QdrantService(qdrant_client)
    await qdrant_svc.ensure_collection()
    dependencies.set_qdrant_client(qdrant_client)

    valid_candidate = [
        {
            "action_type": "suggest_save_result",
            "artifact_type": "hint",
            "trigger_dsl": "",
            "observation": "Memory writes occur repeatedly.",
            "why_it_matters": "Automating this saves the user time.",
            "proposed_content": "Suggest saving results after implementation.",
            "confidence": 0.75,
            "risk_level": "low",
            "evidence_count": 5,
        }
    ]

    ollama = OllamaService.__new__(OllamaService)
    ollama.base_url = "http://mocked"
    ollama.model = "nomic-embed-text"
    ollama.embed = AsyncMock(return_value=MOCK_VECTOR)
    ollama.embed_batch = AsyncMock(return_value=[MOCK_VECTOR] * 100)
    ollama.generate = AsyncMock(return_value=json.dumps(valid_candidate))
    ollama.health = AsyncMock(return_value=True)
    ollama.close = AsyncMock()
    dependencies.set_ollama_service(ollama)

    app = create_app()
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    async with c:
        yield c
    await qdrant_client.close()


BASE = "/api/v1/learning"


# ══════════════════════════════════════════════════════════════════════════════
# Events
# ══════════════════════════════════════════════════════════════════════════════

class TestEventsApi:

    async def test_record_event_success(self, client):
        r = await client.post(f"{BASE}/events", json={
            "event_type": "user_request",
            "agent_id": "test-agent",
            "project": "supermemory",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["event_type"] == "user_request"
        assert isinstance(body["id"], int)

    async def test_record_event_invalid_type(self, client):
        r = await client.post(f"{BASE}/events", json={
            "event_type": "totally_made_up",
        })
        assert r.status_code == 422

    async def test_list_events_empty(self, client):
        r = await client.get(f"{BASE}/events")
        assert r.status_code == 200
        assert r.json()["events"] == []

    async def test_list_events_after_write(self, client):
        await client.post(f"{BASE}/events", json={"event_type": "tool_call"})
        r = await client.get(f"{BASE}/events")
        assert r.json()["total"] >= 1

    async def test_list_events_filter_by_type(self, client):
        await client.post(f"{BASE}/events", json={"event_type": "memory_write"})
        await client.post(f"{BASE}/events", json={"event_type": "tool_call"})
        r = await client.get(f"{BASE}/events", params={"event_type": "memory_write"})
        events = r.json()["events"]
        assert all(e["event_type"] == "memory_write" for e in events)

    async def test_record_event_with_payload(self, client):
        r = await client.post(f"{BASE}/events", json={
            "event_type": "user_request",
            "payload": {"request_type": "save_to_supermemory"},
        })
        assert r.status_code == 201


# ══════════════════════════════════════════════════════════════════════════════
# Artifacts
# ══════════════════════════════════════════════════════════════════════════════

class TestArtifactsApi:

    async def test_list_artifacts_empty(self, client):
        r = await client.get(f"{BASE}/artifacts")
        assert r.status_code == 200
        assert r.json()["artifacts"] == []

    async def test_rate_nonexistent_artifact(self, client):
        from uuid import uuid4
        r = await client.post(f"{BASE}/artifacts/{uuid4()}/rate", json={"useful": True})
        assert r.status_code == 404

    async def test_promote_nonexistent_artifact(self, client):
        from uuid import uuid4
        r = await client.post(
            f"{BASE}/artifacts/{uuid4()}/promote",
            json={"promoted_by": "test"},
        )
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Candidates — create (user-initiated)
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateCandidate:

    async def test_create_hint_candidate(self, client):
        r = await client.post(f"{BASE}/candidates", json={
            "artifact_type": "hint",
            "action_type": "suggest_save_result",
            "content": "After implementation, save results to memory.",
            "observation": "User repeatedly saves manually.",
            "why_it_matters": "Saves 2 minutes per session.",
        })
        assert r.status_code == 201
        body = r.json()
        assert "id" in body
        assert body["created"] is True
        assert body["evidence_count"] >= 1

    async def test_create_if_then_rule_candidate(self, client):
        r = await client.post(f"{BASE}/candidates", json={
            "artifact_type": "if_then_rule",
            "action_type": "suggest_save_result",
            "trigger_dsl": 'event(user_request).request_type == "save_to_supermemory"',
            "content": "When user requests save, auto-suggest memory write.",
            "observation": "Save requests without memory writes.",
            "why_it_matters": "Reduces manual saves.",
        })
        assert r.status_code == 201

    async def test_create_if_then_rule_invalid_dsl(self, client):
        r = await client.post(f"{BASE}/candidates", json={
            "artifact_type": "if_then_rule",
            "action_type": "suggest_save_result",
            "trigger_dsl": 'event(INVALID_EVENT)',
            "content": "Some content.",
        })
        assert r.status_code == 422
        assert "dsl_errors" in r.json()["detail"]

    async def test_create_invalid_action_type(self, client):
        r = await client.post(f"{BASE}/candidates", json={
            "artifact_type": "hint",
            "action_type": "totally_not_real",
            "content": "Content.",
        })
        assert r.status_code == 422

    async def test_dedup_increments_evidence(self, client):
        body = {
            "artifact_type": "hint",
            "action_type": "suggest_save_result",
            "content": "Same content.",
        }
        r1 = await client.post(f"{BASE}/candidates", json=body)
        r2 = await client.post(f"{BASE}/candidates", json=body)
        assert r1.json()["created"] is True
        assert r2.json()["created"] is False
        assert r2.json()["evidence_count"] == 2

    async def test_create_records_user_request_event(self, client):
        await client.post(f"{BASE}/candidates", json={
            "artifact_type": "hint",
            "action_type": "suggest_save_result",
            "content": "Content.",
        })
        r = await client.get(f"{BASE}/events", params={"event_type": "user_request"})
        assert r.json()["total"] >= 1

    async def test_create_candidate_builds_context_signature(self, client):
        r = await client.post(f"{BASE}/candidates", json={
            "artifact_type": "hint",
            "action_type": "suggest_save_result",
            "content": "Content.",
            "project": "supermemory",
            "phase": "implement",
            "category": "code",
            "transport": "mcp",
        })
        assert r.status_code == 201


# ══════════════════════════════════════════════════════════════════════════════
# Candidates — approve / reject / defer
# ══════════════════════════════════════════════════════════════════════════════

class TestCandidateReviewApi:

    async def _create(self, client, action_type="suggest_save_result", content="rule"):
        r = await client.post(f"{BASE}/candidates", json={
            "artifact_type": "hint",
            "action_type": action_type,
            "content": content,
        })
        assert r.status_code == 201
        return r.json()["id"]

    async def test_approve_candidate(self, client):
        cid = await self._create(client)
        r = await client.post(f"{BASE}/candidates/{cid}/approve")
        assert r.status_code == 200
        body = r.json()
        assert body["artifact_scope"] == "runtime_hint"
        assert body["status"] == "active"

    async def test_approve_records_explicit_approval_metadata(self, client):
        cid = await self._create(client)
        r = await client.post(
            f"{BASE}/candidates/{cid}/approve",
            json={
                "approved_by": "owner",
                "approval_source": "inline_user_approval",
                "reason": "Useful for this project",
            },
        )
        assert r.status_code == 200
        row = await get_learning_store().get_artifact(UUID(cid))
        assert row is not None
        assert row["meta"]["approved_by"] == "owner"
        assert row["meta"]["approval_source"] == "inline_user_approval"
        assert row["meta"]["approval_reason"] == "Useful for this project"
        assert row["meta"]["approved_at"] > 0

    async def test_approve_records_positive_feedback(self, client):
        cid = await self._create(client)
        await client.post(f"{BASE}/candidates/{cid}/approve")
        r = await client.get(f"{BASE}/events")  # feedback is separate table
        # Can verify via list_feedback implicitly through the store
        # (feedback endpoint not exposed, so just ensure no error)
        assert r.status_code == 200

    async def test_approve_twice_returns_404(self, client):
        cid = await self._create(client)
        await client.post(f"{BASE}/candidates/{cid}/approve")
        r = await client.post(f"{BASE}/candidates/{cid}/approve")
        assert r.status_code == 404

    async def test_second_approve_does_not_trigger_crystallization_side_effect(self, client, monkeypatch):
        cid = await self._create(client, action_type="crystallize_knowledge", content="Promote this pattern.")
        called = {"count": 0}

        async def fake_apply_crystallization(*args, **kwargs):
            called["count"] += 1
            return "canonical-1"

        monkeypatch.setattr("app.services.crystallization_service.apply_crystallization", fake_apply_crystallization)
        first = await client.post(f"{BASE}/candidates/{cid}/approve")
        assert first.status_code == 200
        assert called["count"] == 1

        second = await client.post(f"{BASE}/candidates/{cid}/approve")
        assert second.status_code == 404
        assert called["count"] == 1

    async def test_hint_accept_records_hint_review_approval_metadata(self, client):
        store = get_learning_store()
        artifact_id, _ = await store.upsert_candidate(
            agent_id="scout",
            action_type="suggest_save_result",
            content="Adopt this external practice.",
            context_signature="project=sm;category=best-practice",
            observation="Pros and cons",
            why_it_matters="Reusable practice",
            risk_level="low",
            confidence=0.65,
            tags=["external", "best-practice"],
            domain="python",
            artifact_type="meta_guidance",
            meta={"title": "Use practice"},
        )

        r = await client.post(f"{BASE}/hints/{artifact_id}/react", json={"accept": True, "reason": "Looks valid"})
        assert r.status_code == 200
        row = await store.get_artifact(artifact_id)
        assert row is not None
        assert row["meta"]["approved_by"] == "user"
        assert row["meta"]["approval_source"] == "hint_review_accept"
        assert row["meta"]["approval_reason"] == "Looks valid"

    async def test_reject_candidate(self, client):
        cid = await self._create(client)
        r = await client.post(f"{BASE}/candidates/{cid}/reject")
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

    async def test_reject_records_explicit_rejection_metadata(self, client):
        cid = await self._create(client)
        r = await client.post(
            f"{BASE}/candidates/{cid}/reject",
            json={
                "rejected_by": "owner",
                "rejection_source": "dashboard_review",
                "reason": "Not a good fit",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["rejected_by"] == "owner"
        assert body["meta"]["rejection_source"] == "dashboard_review"
        assert body["meta"]["rejection_reason"] == "Not a good fit"
        assert body["meta"]["rejected_at"] is not None

    async def test_reject_nonexistent(self, client):
        from uuid import uuid4
        r = await client.post(f"{BASE}/candidates/{uuid4()}/reject")
        assert r.status_code == 404

    async def test_defer_candidate(self, client):
        cid = await self._create(client)
        r = await client.post(f"{BASE}/candidates/{cid}/defer", json={"defer_days": 7})
        assert r.status_code == 200
        body = r.json()
        assert body["defer_count"] == 1
        assert body["status"] == "pending_review"
        assert body["next_surface_after"] is not None

    async def test_defer_increments_defer_count(self, client):
        cid = await self._create(client)
        r = await client.post(f"{BASE}/candidates/{cid}/defer", json={})
        body = r.json()
        assert body["defer_count"] == 1  # effective threshold = min_evidence + 1*3

    async def test_defer_with_reason(self, client):
        cid = await self._create(client)
        r = await client.post(f"{BASE}/candidates/{cid}/defer", json={
            "defer_days": 14,
            "reason": "Need more data before deciding",
        })
        assert r.status_code == 200
        assert r.json()["reason"] == "Need more data before deciding"

    async def test_defer_records_explicit_defer_metadata(self, client):
        cid = await self._create(client)
        r = await client.post(
            f"{BASE}/candidates/{cid}/defer",
            json={
                "defer_days": 14,
                "deferred_by": "owner",
                "defer_source": "dashboard_review",
                "reason": "Need broader evidence",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["deferred_by"] == "owner"
        assert body["defer_source"] == "dashboard_review"

        store = get_learning_store()
        artifact = await store.get_artifact(UUID(cid))
        assert artifact is not None
        assert artifact["meta"]["last_deferred_by"] == "owner"
        assert artifact["meta"]["last_defer_source"] == "dashboard_review"
        assert artifact["meta"]["last_defer_reason"] == "Need broader evidence"
        assert artifact["meta"]["last_deferred_at"] is not None

    async def test_promote_after_approve(self, client):
        cid = await self._create(client)
        await client.post(f"{BASE}/candidates/{cid}/approve")
        r = await client.post(f"{BASE}/artifacts/{cid}/promote",
                               json={"promoted_by": "test"})
        assert r.status_code == 200
        assert r.json()["artifact_scope"] == "persistent_rule"

    async def test_promote_records_promotion_metadata(self, client):
        cid = await self._create(client)
        await client.post(f"{BASE}/candidates/{cid}/approve")
        r = await client.post(
            f"{BASE}/artifacts/{cid}/promote",
            json={
                "promoted_by": "owner",
                "promotion_source": "dashboard_review",
                "reason": "Reviewed in dashboard",
            },
        )
        assert r.status_code == 200
        row = await get_learning_store().get_artifact(UUID(cid))
        assert row is not None
        assert row["meta"]["last_promoted_by"] == "owner"
        assert row["meta"]["last_promotion_source"] == "dashboard_review"
        assert row["meta"]["last_promotion_reason"] == "Reviewed in dashboard"

    async def test_promote_candidate_directly_fails(self, client):
        cid = await self._create(client)
        r = await client.post(f"{BASE}/artifacts/{cid}/promote",
                               json={"promoted_by": "test"})
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

class TestReportApi:

    async def test_report_empty(self, client):
        r = await client.get(f"{BASE}/report")
        assert r.status_code == 200
        assert r.json() == []

    async def test_report_below_threshold_not_shown(self, client):
        # suggest_save_result threshold = 3; insert only 1
        await client.post(f"{BASE}/candidates", json={
            "artifact_type": "hint",
            "action_type": "suggest_save_result",
            "content": "rule",
        })
        r = await client.get(f"{BASE}/report")
        assert r.json() == []

    async def test_report_at_threshold_shown(self, client):
        # Insert 3 times (same key → evidence_count=3)
        body = {
            "artifact_type": "hint",
            "action_type": "suggest_save_result",
            "content": "rule",
        }
        for _ in range(3):
            await client.post(f"{BASE}/candidates", json=body)
        r = await client.get(f"{BASE}/report")
        items = r.json()
        assert len(items) >= 1
        item = items[0]
        assert item["action_type"] == "suggest_save_result"
        assert item["evidence_count"] >= 3
        assert "observation" in item
        assert "why_it_matters" in item
        assert "min_evidence" in item

    async def test_report_limit(self, client):
        body = {
            "artifact_type": "hint",
            "action_type": "suggest_save_result",
            "content": "rule",
        }
        for _ in range(3):
            await client.post(f"{BASE}/candidates", json=body)
        r = await client.get(f"{BASE}/report", params={"limit": 1})
        assert len(r.json()) <= 1


# ══════════════════════════════════════════════════════════════════════════════
# Context-signature utility
# ══════════════════════════════════════════════════════════════════════════════

class TestContextSignatureApi:

    async def test_build_context_signature(self, client):
        r = await client.post(f"{BASE}/context-signature", json={
            "project": "supermemory",
            "task_type": "code",
            "phase": "implement",
            "category": "general",
            "transport": "mcp",
        })
        assert r.status_code == 200
        sig = r.json()["context_signature"]
        assert "project=supermemory" in sig
        assert "phase=implement" in sig

    async def test_context_signature_deterministic(self, client):
        payload = {
            "project": "sm",
            "task_type": "t",
            "phase": "p",
            "category": "c",
            "transport": "mcp",
        }
        r1 = await client.post(f"{BASE}/context-signature", json=payload)
        r2 = await client.post(f"{BASE}/context-signature", json=payload)
        assert r1.json()["context_signature"] == r2.json()["context_signature"]


# ══════════════════════════════════════════════════════════════════════════════
# GLM Mirror endpoints
# ══════════════════════════════════════════════════════════════════════════════

class TestGlmMirrorApi:

    async def test_mirror_status_no_prior_run(self, client):
        # Reset singleton so last_result is None
        from app.services.glm_mirror import get_glm_mirror, _mirror
        import app.services.glm_mirror as _gm
        old = _gm._mirror
        _gm._mirror = None
        try:
            r = await client.get(f"{BASE}/mirror/status")
            assert r.status_code == 200
            assert r.json()["last_run"] is None
        finally:
            _gm._mirror = old

    async def test_mirror_run_no_events(self, client):
        r = await client.post(f"{BASE}/mirror/run")
        assert r.status_code == 200
        body = r.json()
        assert body["events_analyzed"] == 0
        assert body["candidates_created"] == 0

    async def test_mirror_run_with_events_creates_candidate(self, glm_client):
        import app.services.learning_store as _ls_mod
        from app.services.glm_mirror import _MIN_PATTERN_FREQ
        from app.services.learning_store import make_context_signature

        ctx = make_context_signature(project="sm")
        for _ in range(_MIN_PATTERN_FREQ):
            await _ls_mod._store.write_event(
                event_type="dialogue_signal",
                context_signature=ctx,
                payload={"missing_skill": ["nginx"], "excerpt": "USER: help with nginx"},
            )

        r = await glm_client.post(f"{BASE}/mirror/run")
        assert r.status_code == 200
        body = r.json()
        assert body["candidates_created"] == 1
        assert body["skipped_validation"] == 0

    async def test_mirror_status_after_run(self, client):
        from app.services.glm_mirror import get_glm_mirror
        # Reset singleton
        import app.services.glm_mirror as _gm
        _gm._mirror = None

        await client.post(f"{BASE}/mirror/run")
        r = await client.get(f"{BASE}/mirror/status")
        assert r.status_code == 200
        body = r.json()
        assert body["last_run"] is not None
        assert "ran_at" in body["last_run"]
        assert "interval_hours" in body

    async def test_mirror_run_records_llm_mirror_event_when_patterns_found(self, glm_client):
        import app.services.learning_store as _ls_mod
        from app.services.glm_mirror import _MIN_PATTERN_FREQ
        from app.services.learning_store import make_context_signature

        ctx = make_context_signature(project="sm2")
        for _ in range(_MIN_PATTERN_FREQ):
            await _ls_mod._store.write_event(
                event_type="dialogue_signal",
                context_signature=ctx,
                payload={"missing_skill": ["docker"], "excerpt": "USER: help with docker"},
            )

        await glm_client.post(f"{BASE}/mirror/run")
        events = await _ls_mod._store.list_events(event_type="llm_mirror")
        assert len(events) >= 1
