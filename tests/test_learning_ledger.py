"""
Unit tests for the Learning Ledger core components:
  - trigger_dsl: DSL validation (no DB)
  - learning_store: events, feedback, artifacts, candidates, dedup, report, ledger_mirror
  - glm_mirror: pattern detection, JSON parsing, validation, upsert flow
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from app.services.learning_store import (
    LearningStore,
    make_artifact_key,
    make_context_signature,
    min_evidence_for,
)
from app.services.trigger_dsl import (
    ALLOWED_ACTION_TYPES,
    validate_if_then_rule,
    validate_trigger,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def store(tmp_path: Path):
    s = LearningStore(db_path=tmp_path / "learning.db")
    yield s
    await s.aclose()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _insert_candidate(store: LearningStore, *, action_type="suggest_save_result",
                             content="test rule", evidence=1):
    uid, created = await store.upsert_candidate(
        agent_id="test",
        action_type=action_type,
        content=content,
    )
    # Manually bump evidence_count if needed
    if evidence > 1:
        for _ in range(evidence - 1):
            uid, _ = await store.upsert_candidate(
                agent_id="test",
                action_type=action_type,
                content=content,
            )
    return uid


# ══════════════════════════════════════════════════════════════════════════════
# Trigger DSL validator
# ══════════════════════════════════════════════════════════════════════════════

class TestTriggerDsl:

    def test_valid_simple_event(self):
        assert validate_trigger('event(user_request)') == []

    def test_valid_field_eq(self):
        assert validate_trigger('event(user_request).request_type == "save_to_supermemory"') == []

    def test_valid_field_in(self):
        assert validate_trigger('event(tool_call).tool_name in ["run_tests", "pytest"]') == []

    def test_valid_negation(self):
        assert validate_trigger('not event(memory_write)') == []

    def test_valid_compound_and(self):
        errs = validate_trigger(
            'event(user_request).request_type == "save_to_supermemory" and not event(memory_write)'
        )
        assert errs == []

    def test_valid_within(self):
        assert validate_trigger('within(300, event(tool_call).tool_name == "run_tests")') == []

    def test_invalid_event_type(self):
        errs = validate_trigger('event(totally_unknown)')
        assert any("unknown event_type" in e for e in errs)

    def test_invalid_field_for_type(self):
        errs = validate_trigger('event(episode_end).nonexistent_field == "x"')
        assert any("not allowed" in e for e in errs)

    def test_invalid_op_value_not_quoted(self):
        errs = validate_trigger('event(user_request).request_type == save_to_supermemory')
        assert errs  # unquoted string

    def test_in_requires_json_list(self):
        errs = validate_trigger('event(user_request).request_type in "not_a_list"')
        assert errs

    def test_empty_trigger_invalid(self):
        errs = validate_trigger("")
        assert errs

    def test_validate_if_then_rule_valid(self):
        errs = validate_if_then_rule(
            'event(user_request).request_type == "save_to_supermemory"',
            "suggest_save_result",
        )
        assert errs == []

    def test_validate_if_then_rule_bad_action(self):
        errs = validate_if_then_rule(
            'event(user_request)',
            "definitely_not_an_action",
        )
        assert any("action_type" in e for e in errs)

    def test_validate_if_then_rule_bad_trigger_and_action(self):
        errs = validate_if_then_rule("event(bad_event)", "bad_action")
        assert len(errs) >= 2

    def test_within_negative_seconds(self):
        errs = validate_trigger('within(-5, event(tool_call))')
        assert errs

    def test_within_nested_invalid_event(self):
        errs = validate_trigger('within(60, event(unknown_type))')
        assert any("unknown event_type" in e for e in errs)


# ══════════════════════════════════════════════════════════════════════════════
# Context signature & dedup key
# ══════════════════════════════════════════════════════════════════════════════

class TestHelpers:

    def test_context_signature_deterministic(self):
        sig1 = make_context_signature(project="x", task_type="code", phase="impl",
                                       category="general", transport="mcp")
        sig2 = make_context_signature(task_type="code", project="x", phase="impl",
                                       category="general", transport="mcp")
        assert sig1 == sig2

    def test_context_signature_format(self):
        sig = make_context_signature(project="sm", task_type="t", phase="p",
                                      category="c", transport="mcp")
        assert "project=sm" in sig
        assert "phase=p" in sig

    def test_context_signature_optional_agent(self):
        sig_no = make_context_signature(project="x")
        sig_ag = make_context_signature(project="x", agent="claude")
        assert "agent=claude" in sig_ag
        assert "agent" not in sig_no

    def test_artifact_key_same_inputs(self):
        k1 = make_artifact_key("suggest_save_result", 'event(user_request)', "ctx=a")
        k2 = make_artifact_key("suggest_save_result", 'event(user_request)', "ctx=a")
        assert k1 == k2

    def test_artifact_key_different_action(self):
        k1 = make_artifact_key("suggest_save_result", "", "ctx=a")
        k2 = make_artifact_key("run_tests", "", "ctx=a")
        assert k1 != k2

    def test_artifact_key_trigger_normalized(self):
        k1 = make_artifact_key("run_tests", 'event(tool_call) and not event(memory_write)', "c")
        k2 = make_artifact_key("run_tests", 'not event(memory_write) and event(tool_call)', "c")
        assert k1 == k2

    def test_min_evidence_known(self):
        assert min_evidence_for("suggest_save_result") == 3
        assert min_evidence_for("auto_save_result") == 5

    def test_min_evidence_unknown_falls_back(self):
        assert min_evidence_for("nonexistent_action") == 3


# ══════════════════════════════════════════════════════════════════════════════
# LearningStore — Events
# ══════════════════════════════════════════════════════════════════════════════

class TestLearningStoreEvents:

    @pytest.mark.asyncio
    async def test_write_and_list_event(self, store):
        row_id = await store.write_event(event_type="user_request", agent_id="a", project="p")
        assert isinstance(row_id, int) and row_id > 0
        events = await store.list_events()
        assert any(e["event_type"] == "user_request" for e in events)

    @pytest.mark.asyncio
    async def test_list_events_filter_agent(self, store):
        await store.write_event(event_type="tool_call", agent_id="agent-x")
        await store.write_event(event_type="tool_call", agent_id="agent-y")
        events = await store.list_events(agent_id="agent-x")
        assert all(e["agent_id"] == "agent-x" for e in events)

    @pytest.mark.asyncio
    async def test_list_events_filter_type(self, store):
        await store.write_event(event_type="memory_write", agent_id="a")
        await store.write_event(event_type="tool_call", agent_id="a")
        events = await store.list_events(event_type="memory_write")
        assert all(e["event_type"] == "memory_write" for e in events)

    @pytest.mark.asyncio
    async def test_list_events_since_ts(self, store):
        past = time.time() - 3600
        await store.write_event(event_type="episode_start", agent_id="a")
        events = await store.list_events(since_ts=past)
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_count_events(self, store):
        ctx = make_context_signature(project="sm", task_type="t", phase="p",
                                      category="c", transport="mcp")
        since = time.time() - 1
        for _ in range(4):
            await store.write_event(event_type="tool_call", context_signature=ctx)
        count = await store.count_events("tool_call", ctx, since)
        assert count == 4

    @pytest.mark.asyncio
    async def test_event_payload_stored(self, store):
        await store.write_event(event_type="user_request",
                                 payload={"request_type": "save_to_supermemory"})
        events = await store.list_events(event_type="user_request")
        import json
        p = json.loads(events[0]["payload_json"])
        assert p["request_type"] == "save_to_supermemory"


# ══════════════════════════════════════════════════════════════════════════════
# LearningStore — Feedback
# ══════════════════════════════════════════════════════════════════════════════

class TestLearningStoreFeedback:

    @pytest.mark.asyncio
    async def test_write_and_list_feedback(self, store):
        row_id = await store.write_feedback(valence="positive", source="user")
        assert isinstance(row_id, int) and row_id > 0
        fb = await store.list_feedback()
        assert any(f["valence"] == "positive" for f in fb)

    @pytest.mark.asyncio
    async def test_feedback_linked_to_artifact(self, store):
        art_id = await _insert_candidate(store)
        await store.write_feedback(valence="negative", artifact_id=str(art_id), source="user")
        fb = await store.list_feedback(artifact_id=str(art_id))
        assert len(fb) == 1
        assert fb[0]["valence"] == "negative"

    @pytest.mark.asyncio
    async def test_feedback_magnitude(self, store):
        await store.write_feedback(valence="positive", magnitude=0.9)
        fb = await store.list_feedback()
        assert abs(fb[0]["magnitude"] - 0.9) < 0.01


# ══════════════════════════════════════════════════════════════════════════════
# LearningStore — Candidate lifecycle
# ══════════════════════════════════════════════════════════════════════════════

class TestCandidateLifecycle:

    @pytest.mark.asyncio
    async def test_upsert_candidate_creates(self, store):
        uid, created = await store.upsert_candidate(
            agent_id="test", action_type="suggest_save_result", content="save results"
        )
        assert created is True
        art = await store.get_artifact(uid)
        assert art is not None
        assert art["artifact_scope"] == "candidate"
        assert art["evidence_count"] == 1

    @pytest.mark.asyncio
    async def test_upsert_candidate_dedup_increments_evidence(self, store):
        uid1, created1 = await store.upsert_candidate(
            agent_id="test", action_type="suggest_save_result", content="save results"
        )
        uid2, created2 = await store.upsert_candidate(
            agent_id="test", action_type="suggest_save_result", content="save results"
        )
        assert created1 is True
        assert created2 is False
        assert uid1 == uid2
        art = await store.get_artifact(uid1)
        assert art["evidence_count"] == 2

    @pytest.mark.asyncio
    async def test_upsert_candidate_semantic_dedup_across_context_signature(self, store, monkeypatch):
        import app.services.learning_store as ls

        # Module-level flag is read at import time; force-enable for deterministic tests.
        monkeypatch.setattr(ls, "_SEMANTIC_DEDUP_ENABLED", True)

        uid1, created1 = await store.upsert_candidate(
            agent_id="test",
            action_type="run_tests",
            content="Always run tests before committing changes.",
            context_signature="project=a;task_type=ci",
            meta={"supports": ["a"]},
        )
        uid2, created2 = await store.upsert_candidate(
            agent_id="test",
            action_type="run_tests",
            content="Always run tests before committing changes!",
            context_signature="project=b;task_type=ci",
            meta={"supports": ["b"]},
        )

        assert created1 is True
        assert created2 is False
        assert uid1 == uid2

        art = await store.get_artifact(uid1)
        assert art["evidence_count"] == 2
        assert sorted((art.get("meta") or {}).get("supports") or []) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_upsert_different_action_creates_new(self, store):
        uid1, _ = await store.upsert_candidate(
            agent_id="test", action_type="suggest_save_result", content="x"
        )
        uid2, _ = await store.upsert_candidate(
            agent_id="test", action_type="run_tests", content="x"
        )
        assert uid1 != uid2

    @pytest.mark.asyncio
    async def test_approve_candidate(self, store):
        uid = await _insert_candidate(store)
        updated = await store.approve_candidate(uid)
        assert updated is not None
        assert updated["artifact_scope"] == "runtime_hint"
        assert updated["status"] == "active"

    @pytest.mark.asyncio
    async def test_approve_nonexistent_returns_none(self, store):
        result = await store.approve_candidate(uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_approve_already_approved_returns_none(self, store):
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)
        result = await store.approve_candidate(uid)  # no longer a candidate
        assert result is None

    @pytest.mark.asyncio
    async def test_reject_candidate(self, store):
        uid = await _insert_candidate(store)
        updated = await store.reject_candidate(uid)
        assert updated is not None
        assert updated["status"] == "archived"
        assert updated["rejects"] >= 1

    @pytest.mark.asyncio
    async def test_reject_nonexistent_returns_none(self, store):
        assert await store.reject_candidate(uuid4()) is None

    @pytest.mark.asyncio
    async def test_defer_increments_defer_count(self, store):
        uid = await _insert_candidate(store)
        updated = await store.defer_candidate(uid)
        assert updated["defer_count"] == 1
        assert updated["status"] == "pending_review"
        assert updated["next_surface_after"] > time.time()

    @pytest.mark.asyncio
    async def test_defer_raises_effective_threshold(self, store):
        # defer_count is incremented; get_report_candidates applies threshold + defer_count*3
        uid = await _insert_candidate(store, evidence=1)
        art_before = await store.get_artifact(uid)
        assert art_before["defer_count"] == 0
        await store.defer_candidate(uid)
        art_after = await store.get_artifact(uid)
        assert art_after["defer_count"] == 1
        # effective threshold = min_evidence_for() + 1*3 — candidate needs more evidence now

    @pytest.mark.asyncio
    async def test_defer_exponential_backoff(self, store):
        uid = await _insert_candidate(store)
        now = time.time()
        # First defer: ~7 days
        d1 = await store.defer_candidate(uid, defer_days=7)
        delay1 = d1["next_surface_after"] - now
        assert 6.5 * 86400 < delay1 < 7.5 * 86400
        # Second defer: ~14 days
        d2 = await store.defer_candidate(uid, defer_days=7)
        delay2 = d2["next_surface_after"] - now
        assert 13 * 86400 < delay2

    @pytest.mark.asyncio
    async def test_defer_auto_archives_at_max(self, store):
        uid = await _insert_candidate(store)
        # Force max by using defer_days=90 on first defer
        updated = await store.defer_candidate(uid, defer_days=90)
        assert updated["status"] == "archived"

    @pytest.mark.asyncio
    async def test_reject_non_candidate_scope_returns_none(self, store):
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)  # now runtime_hint
        result = await store.reject_candidate(uid)
        assert result is None

    @pytest.mark.asyncio
    async def test_promote_candidate_blocked(self, store):
        uid = await _insert_candidate(store)
        # promote() must not work for candidates — use approve() instead
        result = await store.promote_artifact(uid, promoted_by="test")
        assert result is None

    @pytest.mark.asyncio
    async def test_promote_runtime_hint_to_persistent_rule(self, store):
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)
        updated = await store.promote_artifact(uid, promoted_by="test")
        assert updated is not None
        assert updated["artifact_scope"] == "persistent_rule"


# ══════════════════════════════════════════════════════════════════════════════
# LearningStore — Report (get_report_candidates)
# ══════════════════════════════════════════════════════════════════════════════

class TestReportCandidates:

    @pytest.mark.asyncio
    async def test_below_threshold_not_surfaced(self, store):
        # suggest_save_result threshold = 3; insert only 1
        await _insert_candidate(store, action_type="suggest_save_result", evidence=1)
        report = await store.get_report_candidates()
        assert report == []

    @pytest.mark.asyncio
    async def test_at_threshold_surfaced(self, store):
        threshold = min_evidence_for("suggest_save_result")  # 3
        await _insert_candidate(store, action_type="suggest_save_result", evidence=threshold)
        report = await store.get_report_candidates()
        assert len(report) >= 1

    @pytest.mark.asyncio
    async def test_deferred_not_surfaced_before_next_surface_after(self, store):
        threshold = min_evidence_for("suggest_save_result")
        uid = await _insert_candidate(store, action_type="suggest_save_result",
                                       evidence=threshold)
        await store.defer_candidate(uid, defer_days=7)
        report = await store.get_report_candidates()
        assert all(r["id"] != str(uid) for r in report)

    @pytest.mark.asyncio
    async def test_approved_not_in_report(self, store):
        threshold = min_evidence_for("suggest_save_result")
        uid = await _insert_candidate(store, action_type="suggest_save_result",
                                       evidence=threshold)
        await store.approve_candidate(uid)
        report = await store.get_report_candidates()
        assert all(r["id"] != str(uid) for r in report)

    @pytest.mark.asyncio
    async def test_report_limit(self, store):
        threshold = min_evidence_for("suggest_save_result")
        for i in range(5):
            await _insert_candidate(store, action_type="suggest_save_result",
                                     content=f"rule {i}", evidence=threshold)
        report = await store.get_report_candidates(limit=2)
        assert len(report) <= 2


# ══════════════════════════════════════════════════════════════════════════════
# LearningStore — Ledger mirror
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerMirror:

    @pytest.mark.asyncio
    async def test_exact_context_match(self, store):
        ctx = make_context_signature(project="sm", task_type="code", phase="impl",
                                      category="general", transport="mcp")
        uid = await _insert_candidate(store, action_type="suggest_save_result", evidence=1)
        # Approve → promote to persistent_rule → promote to promoted_pattern
        await store.approve_candidate(uid)
        await store.promote_artifact(uid, promoted_by="test")  # → persistent_rule
        await store.promote_artifact(uid, promoted_by="test")  # → promoted_pattern
        # Update context_signature manually to match
        with store._lock:
            store._conn.execute(
                "UPDATE artifacts SET context_signature = ?, status = 'active' WHERE id = ?",
                (ctx, str(uid)),
            )
            store._conn.commit()
        results = await store.ledger_mirror(ctx)
        assert any(r["id"] == str(uid) for r in results)

    @pytest.mark.asyncio
    async def test_empty_when_no_promoted_patterns(self, store):
        ctx = make_context_signature(project="sm")
        results = await store.ledger_mirror(ctx)
        assert results == []

    @pytest.mark.asyncio
    async def test_non_matching_context_excluded(self, store):
        ctx_a = make_context_signature(project="alpha")
        ctx_b = make_context_signature(project="beta")
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)
        await store.promote_artifact(uid, "t")
        await store.promote_artifact(uid, "t")
        with store._lock:
            store._conn.execute(
                "UPDATE artifacts SET context_signature = ?, status = 'active' WHERE id = ?",
                (ctx_a, str(uid)),
            )
            store._conn.commit()
        results = await store.ledger_mirror(ctx_b)
        assert all(r["id"] != str(uid) for r in results)


# ══════════════════════════════════════════════════════════════════════════════
# LearningStore — Rate + Decay
# ══════════════════════════════════════════════════════════════════════════════

class TestRateAndDecay:

    @pytest.mark.asyncio
    async def test_rate_artifact_useful(self, store):
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)
        updated = await store.rate_artifact(uid, useful=True)
        assert updated["useful_votes"] == 1
        assert updated["confidence"] > 0

    @pytest.mark.asyncio
    async def test_rate_artifact_not_useful(self, store):
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)
        updated = await store.rate_artifact(uid, useful=False)
        assert updated["not_useful_votes"] == 1

    @pytest.mark.asyncio
    async def test_rate_nonexistent_returns_none(self, store):
        assert await store.rate_artifact(uuid4(), useful=True) is None

    @pytest.mark.asyncio
    async def test_decay_stale_artifact(self, store):
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)
        # Set updated_at far in the past
        with store._lock:
            store._conn.execute(
                "UPDATE artifacts SET updated_at = ? WHERE id = ?",
                (time.time() - 40 * 86400, str(uid)),
            )
            store._conn.commit()
        count = await store.decay_stale_artifacts(inactivity_days=30)
        assert count >= 1
        art = await store.get_artifact(uid)
        assert art["status"] == "archived"

    @pytest.mark.asyncio
    async def test_decay_skips_recent(self, store):
        uid = await _insert_candidate(store)
        await store.approve_candidate(uid)
        count = await store.decay_stale_artifacts(inactivity_days=30)
        assert count == 0


# ══════════════════════════════════════════════════════════════════════════════
# GLM Mirror
# ══════════════════════════════════════════════════════════════════════════════

class TestGlmMirror:

    @pytest.mark.asyncio
    async def test_no_events_returns_empty(self, store):
        from app.services.glm_mirror import GlmMirror
        mirror = GlmMirror()
        ollama = AsyncMock()
        result = await mirror.run(ollama, store)
        assert result.events_analyzed == 0
        assert result.candidates_created == 0
        ollama.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_below_min_freq_skipped(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        mirror = GlmMirror()
        # Insert fewer events than threshold
        for _ in range(_MIN_PATTERN_FREQ - 1):
            await store.write_event(event_type="tool_call", context_signature="ctx=x")
        ollama = AsyncMock()
        result = await mirror.run(ollama, store)
        assert result.patterns_found == 0
        ollama.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_llm_response_creates_candidate(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        # Seed enough events to trigger analysis
        ctx = make_context_signature(project="sm", task_type="t", phase="p",
                                      category="c", transport="mcp")
        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(event_type="memory_write", context_signature=ctx)

        valid_candidates = [
            {
                "action_type": "suggest_save_result",
                "artifact_type": "hint",
                "trigger_dsl": "",
                "observation": "Memory writes occur frequently in this context.",
                "why_it_matters": "Auto-saving saves the user repeated manual effort.",
                "proposed_content": "Suggest saving results after each implementation step.",
                "confidence": 0.75,
                "risk_level": "low",
                "evidence_count": 5,
            }
        ]
        import json
        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value=json.dumps(valid_candidates))

        mirror = GlmMirror()
        result = await mirror.run(ollama, store)
        assert result.candidates_created == 1
        assert result.candidates_updated == 0
        assert result.skipped_validation == 0

    @pytest.mark.asyncio
    async def test_duplicate_key_increments_evidence(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        import json
        ctx = make_context_signature(project="sm")
        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(event_type="memory_write", context_signature=ctx)

        candidate = {
            "action_type": "suggest_save_result",
            "artifact_type": "hint",
            "trigger_dsl": "",
            "observation": "Repeated observation.",
            "why_it_matters": "Saves time.",
            "proposed_content": "Auto-save after code changes.",
            "confidence": 0.7,
            "risk_level": "low",
            "evidence_count": 3,
        }
        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value=json.dumps([candidate]))
        mirror = GlmMirror()

        r1 = await mirror.run(ollama, store)
        r2 = await mirror.run(ollama, store)
        assert r1.candidates_created == 1
        assert r2.candidates_updated == 1  # dedup, evidence++
        assert r2.candidates_created == 0

    @pytest.mark.asyncio
    async def test_invalid_json_records_error(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        ctx = make_context_signature(project="sm")
        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(event_type="tool_call", context_signature=ctx)

        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value="this is not json at all")
        mirror = GlmMirror()
        result = await mirror.run(ollama, store)
        assert result.candidates_created == 0
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_invalid_action_type_skipped(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        import json
        ctx = make_context_signature(project="sm")
        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(event_type="memory_write", context_signature=ctx)

        bad_candidate = {
            "action_type": "DELETE_EVERYTHING",  # not in whitelist
            "artifact_type": "hint",
            "trigger_dsl": "",
            "observation": "Something.",
            "why_it_matters": "Something.",
            "proposed_content": "Do something dangerous.",
            "confidence": 0.9,
            "risk_level": "low",
            "evidence_count": 5,
        }
        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value=json.dumps([bad_candidate]))
        mirror = GlmMirror()
        result = await mirror.run(ollama, store)
        assert result.candidates_created == 0
        assert result.skipped_validation == 1

    @pytest.mark.asyncio
    async def test_high_risk_skipped(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        import json
        ctx = make_context_signature(project="sm")
        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(event_type="memory_write", context_signature=ctx)

        high_risk = {
            "action_type": "suggest_save_result",
            "artifact_type": "hint",
            "trigger_dsl": "",
            "observation": "Obs.",
            "why_it_matters": "Why.",
            "proposed_content": "Content.",
            "confidence": 0.8,
            "risk_level": "high",   # blocked
            "evidence_count": 5,
        }
        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value=json.dumps([high_risk]))
        mirror = GlmMirror()
        result = await mirror.run(ollama, store)
        assert result.candidates_created == 0
        assert result.skipped_validation == 1

    @pytest.mark.asyncio
    async def test_empty_observation_skipped(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        import json
        ctx = make_context_signature(project="sm")
        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(event_type="memory_write", context_signature=ctx)

        empty_obs = {
            "action_type": "suggest_save_result",
            "artifact_type": "hint",
            "trigger_dsl": "",
            "observation": "",   # empty — invalid
            "why_it_matters": "Saves time.",
            "proposed_content": "Save content.",
            "confidence": 0.75,
            "risk_level": "low",
            "evidence_count": 4,
        }
        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value=json.dumps([empty_obs]))
        mirror = GlmMirror()
        result = await mirror.run(ollama, store)
        assert result.skipped_validation == 1

    @pytest.mark.asyncio
    async def test_records_llm_mirror_event(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ
        ctx = make_context_signature(project="sm")
        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(event_type="memory_write", context_signature=ctx)

        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value="[]")
        mirror = GlmMirror()
        await mirror.run(ollama, store)
        events = await store.list_events(event_type="llm_mirror")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_dialogue_events_are_preferred_over_tool_telemetry(self, store):
        from app.services.glm_mirror import GlmMirror, _MIN_PATTERN_FREQ

        for _ in range(_MIN_PATTERN_FREQ):
            await store.write_event(
                event_type="tool_call",
                context_signature="category=memory_context;project=supermemory",
                payload={"tool_name": "memory_context"},
            )
            await store.write_event(
                event_type="dialogue_signal",
                context_signature="category=dialogue_signal;project=supermemory",
                payload={
                    "missing_skill": ["nginx"],
                    "successful_pattern": ["use concise reverse proxy checklist"],
                    "excerpt": "USER: help with nginx reverse proxy and SSL",
                },
            )

        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value="[]")
        mirror = GlmMirror()
        result = await mirror.run(ollama, store)

        prompt = ollama.generate.await_args.args[0]
        assert "nginx" in prompt
        assert "memory_context" not in prompt
        assert "dialogue_evidence_absent_fallback_to_telemetry" not in result.warnings

    @pytest.mark.asyncio
    async def test_last_result_stored(self, store):
        from app.services.glm_mirror import GlmMirror
        mirror = GlmMirror()
        assert mirror.last_result() is None
        ollama = AsyncMock()
        ollama.generate = AsyncMock(return_value="[]")
        await mirror.run(ollama, store)
        assert mirror.last_result() is not None


@pytest.mark.asyncio
async def test_migration_dedupes_duplicate_keys_and_adds_unique_index(tmp_path: Path):
    """
    Regression: without a DB-level UNIQUE index, multi-process runs can create duplicate
    artifacts with the same `key`. Migration must collapse them and enforce uniqueness.
    """
    db_path = tmp_path / "learning.db"

    # Create an "old" DB state by dropping the unique index and inserting duplicates.
    s1 = LearningStore(db_path=db_path)
    with s1._lock:
        s1._conn.execute("DROP INDEX IF EXISTS idx_artifacts_key_uniq_nonempty")
        s1._conn.commit()

    key = make_artifact_key("suggest_run_tests", "", "")
    for i in range(3):
        await s1.insert_artifact(
            agent_id="test",
            artifact_type="hint",
            scope="runtime_hint",
            status="active",
            action_type="suggest_run_tests",
            key=key,
            evidence_count=1,
            content=f"dupe-{i}",
        )
    await s1.aclose()

    # Re-open via LearningStore to trigger migration (dedupe + unique index).
    s2 = LearningStore(db_path=db_path)
    with s2._lock:
        n = int(
            s2._conn.execute("SELECT COUNT(*) FROM artifacts WHERE key = ?", (key,)).fetchone()[0]
        )
        evidence = int(
            s2._conn.execute("SELECT evidence_count FROM artifacts WHERE key = ?", (key,)).fetchone()[0]
        )
        idx_rows = s2._conn.execute("PRAGMA index_list('artifacts')").fetchall()
        has_unique = any(
            (r[1] == "idx_artifacts_key_uniq_nonempty") and (int(r[2]) == 1) for r in idx_rows
        )

    assert n == 1
    assert evidence == 3
    assert has_unique is True
    await s2.aclose()
