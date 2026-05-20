import json
from pathlib import Path

import pytest

from app.services.rule_lifecycle_service import (
    RuleLifecycleStore,
    build_rule_candidate_review_packet,
    project_rule_candidates_from_stenographer,
)
from app.services.stenographer_service import StenographerStore


def _start_work(stenographer: StenographerStore) -> None:
    stenographer.start_work_session(
        project="mnemoforge",
        task_id="task-rule-1",
        agent_id="codex",
        session_id="sess-rule",
        work_id="work-rule",
    )


def test_rule_marker_span_projects_to_reviewable_candidate():
    stenographer = StenographerStore(Path(":memory:"))
    lifecycle = RuleLifecycleStore(Path(":memory:"))
    try:
        _start_work(stenographer)
        span = stenographer.record_span(
            project="mnemoforge",
            task_id="task-rule-1",
            agent_id="codex",
            session_id="sess-rule",
            kind="rule_project_candidate",
            source="reasoning_marker",
            content=json.dumps({
                "statement": "MnemoForge pytest must run through the Docker test runner.",
                "rationale": "This avoids Windows ACL failures and live database contamination.",
                "topic_path": "testing/contour",
                "evidence_refs": ["task:task-rule-1"],
                "confidence": 0.9,
                "promotion_hint": "May generalize to declared test contours.",
            }),
        )

        report = project_rule_candidates_from_stenographer(
            store=lifecycle,
            stenographer_store=stenographer,
        )

        assert report.scanned_spans == 1
        assert report.created_candidates == 1
        assert report.skipped_spans == 0
        candidate = report.candidates[0]
        assert candidate.source_span_id == span.span_id
        assert candidate.scope == "project"
        assert candidate.status == "candidate"
        assert candidate.topic_path == "testing/contour"
        assert candidate.statement == "MnemoForge pytest must run through the Docker test runner."
        assert candidate.evidence_refs == ["task:task-rule-1"]
        assert candidate.confidence == 0.9
    finally:
        stenographer.close()
        lifecycle.close()


def test_rule_marker_projection_is_incremental_and_idempotent():
    stenographer = StenographerStore(Path(":memory:"))
    lifecycle = RuleLifecycleStore(Path(":memory:"))
    try:
        _start_work(stenographer)
        stenographer.record_span(
            project="mnemoforge",
            task_id="task-rule-1",
            agent_id="codex",
            session_id="sess-rule",
            kind="rule_canonical_candidate",
            source="reasoning_marker",
            content=(
                "statement: Agents must respect the declared test contour for DB-backed tests.\n"
                "rationale: This prevents accidental live-storage writes.\n"
                "topic_path: testing/contour\n"
                "confidence: 0.8"
            ),
        )

        first = project_rule_candidates_from_stenographer(store=lifecycle, stenographer_store=stenographer)
        second = project_rule_candidates_from_stenographer(store=lifecycle, stenographer_store=stenographer)

        assert first.created_candidates == 1
        assert first.candidates[0].scope == "canonical_candidate"
        assert second.scanned_spans == 0
        assert lifecycle.list_candidates(project="mnemoforge")[0].statement.startswith("Agents must respect")
    finally:
        stenographer.close()
        lifecycle.close()


def test_invalid_rule_marker_is_skipped_without_candidate():
    stenographer = StenographerStore(Path(":memory:"))
    lifecycle = RuleLifecycleStore(Path(":memory:"))
    try:
        _start_work(stenographer)
        stenographer.record_span(
            project="mnemoforge",
            task_id="task-rule-1",
            agent_id="codex",
            session_id="sess-rule",
            kind="rule_project_candidate",
            source="reasoning_marker",
            content="statement: Missing rationale should not become a candidate.",
        )

        report = project_rule_candidates_from_stenographer(store=lifecycle, stenographer_store=stenographer)

        assert report.scanned_spans == 1
        assert report.created_candidates == 0
        assert report.skipped_spans == 1
        assert "rationale is required" in report.errors[0]["error"]
        assert lifecycle.list_candidates(project="mnemoforge") == []
    finally:
        stenographer.close()
        lifecycle.close()


def test_rule_candidate_review_actions_update_status_and_audit_fields():
    stenographer = StenographerStore(Path(":memory:"))
    lifecycle = RuleLifecycleStore(Path(":memory:"))
    try:
        _start_work(stenographer)
        stenographer.record_span(
            project="mnemoforge",
            task_id="task-rule-1",
            agent_id="codex",
            session_id="sess-rule",
            kind="rule_project_candidate",
            source="reasoning_marker",
            content=json.dumps({
                "statement": "Agents should reject duplicate rule candidates.",
                "rationale": "This keeps the governed law pool compact.",
                "topic_path": "rules/review",
            }),
        )
        report = project_rule_candidates_from_stenographer(store=lifecycle, stenographer_store=stenographer)
        candidate_id = report.candidates[0].candidate_id

        rejected = lifecycle.review_candidate(
            candidate_id,
            action="reject",
            reason="Duplicate of an active project law.",
            acted_by="codex",
            source="test",
        )
        assert rejected.previous_status == "candidate"
        assert rejected.new_status == "rejected"
        assert rejected.candidate.status == "rejected"
        assert rejected.candidate.last_review_action == "reject"
        assert rejected.candidate.last_review_reason == "Duplicate of an active project law."
        assert rejected.candidate.last_review_acted_by == "codex"
        assert rejected.candidate.last_review_source == "test"
        assert rejected.candidate.last_review_at is not None
        assert lifecycle.list_candidates(project="mnemoforge", status="candidate") == []

        reopened = lifecycle.review_candidate(
            candidate_id,
            action="reopen",
            reason="Review reopened after new evidence.",
            acted_by="codex",
            source="test",
        )
        assert reopened.previous_status == "rejected"
        assert reopened.new_status == "candidate"
        assert lifecycle.list_candidates(project="mnemoforge", status="candidate")[0].candidate_id == candidate_id
    finally:
        stenographer.close()
        lifecycle.close()


def test_expire_trial_candidates_suppresses_expired_trials():
    lifecycle = RuleLifecycleStore(Path(":memory:"))
    try:
        candidate = lifecycle.create_candidate(
            {
                "project": "mnemoforge",
                "scope": "project",
                "topic_path": "rules/trial",
                "marker_kind": "rule_project_candidate",
                "statement": "Trial rules should expire when they never gain evidence.",
                "rationale": "This keeps temporary rule proposals from becoming permanent noise.",
                "evidence_refs": ["test"],
                "source_task_id": "",
                "source_session_id": "",
                "source_span_id": "span-expired-trial",
                "source_work_id": "",
                "confidence": 0.7,
                "promotion_hint": "",
                "related_rule_hint": None,
                "status": "trial",
                "trial_started_at": 1.0,
                "trial_review_after": 1.0,
                "trial_expires_at": 1.0,
            }
        )

        result = lifecycle.expire_trial_candidates(
            project="mnemoforge",
            reason="Expired test trial.",
            acted_by="codex",
            source="test",
        )

        assert result.expired_count == 1
        assert result.candidates[0].candidate_id == candidate.candidate_id
        assert result.candidates[0].status == "suppressed"
        assert result.candidates[0].last_review_action == "expire_trial"
        assert result.candidates[0].last_review_reason == "Expired test trial."
        assert lifecycle.list_candidates(project="mnemoforge", status="trial") == []
    finally:
        lifecycle.close()


@pytest.mark.asyncio
async def test_rule_candidate_review_packet_flags_existing_law_overlap(client):
    created = await client.post("/api/v1/laws", json={
        "project": "mnemoforge",
        "title": "Docker Test Contour",
        "statement": "MnemoForge pytest must run through the Docker test runner.",
        "rationale": "This avoids Windows ACL failures and live database contamination.",
        "agent_id": "codex",
        "scope": "project",
        "status": "active",
        "confirmed_by": "user",
        "tags": ["docker", "pytest", "testing-contour"],
    })
    assert created.status_code == 201, created.text

    stenographer = StenographerStore(Path(":memory:"))
    lifecycle = RuleLifecycleStore(Path(":memory:"))
    try:
        _start_work(stenographer)
        stenographer.record_span(
            project="mnemoforge",
            task_id="task-rule-1",
            agent_id="codex",
            session_id="sess-rule",
            kind="rule_project_candidate",
            source="reasoning_marker",
            content=json.dumps({
                "statement": "MnemoForge pytest must run through the Docker test runner.",
                "rationale": "This avoids Windows ACL failures and live database contamination.",
                "topic_path": "testing/contour",
                "confidence": 0.9,
            }),
        )
        project_rule_candidates_from_stenographer(store=lifecycle, stenographer_store=stenographer)

        from app.dependencies import get_qdrant
        from app.models.rule_lifecycle import RuleCandidateReviewRequest
        import app.services.rule_lifecycle_service as service

        original_store = service._STORE
        service._STORE = lifecycle
        try:
            packet = await build_rule_candidate_review_packet(
                get_qdrant(),
                RuleCandidateReviewRequest(project="mnemoforge"),
            )
        finally:
            service._STORE = original_store

        assert packet.total_candidates == 1
        item = packet.items[0]
        assert item.recommendation == "revise_existing_law"
        assert item.matching_laws[0].title == "Docker Test Contour"
        assert item.matching_laws[0].score >= 0.55
    finally:
        stenographer.close()
        lifecycle.close()
