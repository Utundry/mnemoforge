import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock

from app.models.law import ProjectLawImportResponse
from app.services import law_import_service
from app.services import rule_lifecycle_service, stenographer_service
from app.services.law_import_service import parse_project_laws_markdown
from app.services.rule_lifecycle_service import RuleLifecycleStore
from app.services.stenographer_service import StenographerStore

PREFIX = "/api/v1"


def test_parse_project_laws_markdown_extracts_structured_laws():
    markdown = """# Project Law

## Law 1: Memory First

Agents must start from project memory before reading code.

This keeps retrieval consistent.

## Law 2: User Sovereignty

Only explicit user approval may activate project truth.
"""
    drafts = parse_project_laws_markdown(markdown, source_path="docs/PROJECT_LAW.md")
    assert len(drafts) == 2
    assert drafts[0].title == "Memory First"
    assert drafts[0].statement == "Agents must start from project memory before reading code."
    assert "retrieval consistent" in drafts[0].rationale
    assert drafts[0].topic_path == "laws/memory-first"
    assert drafts[1].title == "User Sovereignty"


@pytest.mark.asyncio
async def test_ensure_project_laws_from_markdown_if_missing_imports_when_no_active(monkeypatch):
    path = Path("docs/PROJECT_LAW.md")
    assert path.exists()

    async def fake_list_project_laws(*args, **kwargs):
        return []

    async def fake_import_project_laws_from_markdown(**kwargs):
        return ProjectLawImportResponse(
            project=kwargs["project"],
            source_path=kwargs["path"],
            parsed=1,
            created=1,
            skipped_existing=0,
            staged_candidate_revision=0,
            created_ids=["law-1"],
            staged_ids=[],
        )

    monkeypatch.setattr(law_import_service, "list_project_laws", fake_list_project_laws)
    monkeypatch.setattr(
        law_import_service,
        "import_project_laws_from_markdown",
        fake_import_project_laws_from_markdown,
    )

    result = await law_import_service.ensure_project_laws_from_markdown_if_missing(
        qdrant=object(),
        ollama=object(),
        project="mnemoforge",
        path=str(path),
        agent_id="system",
        confirmed_by="system",
        confirmation_source="startup_bootstrap",
        reason="bootstrap",
    )

    assert result is not None
    assert result.project == "mnemoforge"
    assert result.created == 1


@pytest.mark.asyncio
async def test_ensure_project_laws_from_markdown_if_missing_skips_when_active_exists(monkeypatch):
    path = Path("docs/PROJECT_LAW.md")
    assert path.exists()

    async def fake_list_project_laws(*args, **kwargs):
        return [object()]

    import_mock = AsyncMock()

    monkeypatch.setattr(law_import_service, "list_project_laws", fake_list_project_laws)
    monkeypatch.setattr(law_import_service, "import_project_laws_from_markdown", import_mock)

    result = await law_import_service.ensure_project_laws_from_markdown_if_missing(
        qdrant=object(),
        ollama=object(),
        project="mnemoforge",
        path=str(path),
        agent_id="system",
        confirmed_by="system",
        confirmation_source="startup_bootstrap",
        reason="bootstrap",
    )

    assert result is None
    import_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_and_get_project_law(client):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Require explicit approval for deploys",
        "statement": "Agents must not deploy without explicit user approval.",
        "rationale": "Deploys are high-impact actions.",
        "evidence": ["Repeated user instruction", "Past deployment incident"],
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
        "confirmation_source": "seeded_user_truth",
        "tags": ["governance", "deploy"],
    })
    assert created.status_code == 201
    body = created.json()
    assert body["project"] == "alpha"
    assert body["scope"] == "project"
    assert body["status"] == "active"
    assert body["title"] == "Require explicit approval for deploys"
    assert body["statement"] == "Agents must not deploy without explicit user approval."

    fetched = await client.get(f"{PREFIX}/laws/{body['id']}")
    assert fetched.status_code == 200
    fetched_body = fetched.json()
    assert fetched_body["evidence"] == ["Repeated user instruction", "Past deployment incident"]
    assert fetched_body["is_project_local"] is True
    assert fetched_body["confirmed_by"] == "user"
    assert fetched_body["confirmed_at"] is not None


@pytest.mark.asyncio
async def test_list_project_laws_filters_by_project_and_status(client):
    await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Alpha active law",
        "statement": "Alpha agents must cite applied laws in risky changes.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Alpha proposed law",
        "statement": "Alpha proposed law statement.",
        "agent_id": "codex",
        "status": "proposed",
    })
    await client.post(f"{PREFIX}/laws", json={
        "project": "beta",
        "title": "Beta active law",
        "statement": "Beta law statement.",
        "agent_id": "codex",
        "status": "active",
    })

    active = await client.get(f"{PREFIX}/laws?project=alpha&status=active")
    assert active.status_code == 200
    data = active.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "Alpha active law"
    assert data["items"][0]["is_project_local"] is True


@pytest.mark.asyncio
async def test_update_project_law_and_status(client):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Law title",
        "statement": "Initial statement.",
        "agent_id": "codex",
        "status": "proposed",
    })
    law_id = created.json()["id"]

    updated = await client.patch(f"{PREFIX}/laws/{law_id}", json={
        "statement": "Revised statement.",
        "rationale": "Now clarified.",
        "version": "1.1",
        "supported_by": ["mem-1", "mem-2"],
    })
    assert updated.status_code == 200
    body = updated.json()
    assert body["statement"] == "Revised statement."
    assert body["rationale"] == "Now clarified."
    assert body["version"] == "1.1"
    assert body["supported_by"] == ["mem-1", "mem-2"]

    status = await client.patch(f"{PREFIX}/laws/{law_id}/status", json={
        "status": "active",
        "reason": "approved by reviewer",
        "acted_by": "owner",
        "action_source": "dashboard_review",
    })
    assert status.status_code == 400
    assert "require explicit confirmation" in status.json()["detail"].lower()

    confirmed = await client.post(f"{PREFIX}/laws/{law_id}/confirm", json={
        "confirmed_by": "user",
        "confirmation_source": "inline_user_approval",
        "reason": "approved in task discussion",
        "activate": True,
    })
    assert confirmed.status_code == 200
    confirmed_body = confirmed.json()
    assert confirmed_body["status"] == "active"
    assert confirmed_body["confirmed_by"] == "user"
    assert confirmed_body["confirmed_at"] is not None


@pytest.mark.asyncio
async def test_law_status_change_tracks_explicit_action_metadata(client):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Dormant law",
        "statement": "This law may be suppressed later.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created.status_code == 201
    law_id = created.json()["id"]

    suppressed = await client.patch(f"{PREFIX}/laws/{law_id}/status", json={
        "status": "suppressed",
        "reason": "Not applicable for now",
        "acted_by": "owner",
        "action_source": "dashboard_review",
    })
    assert suppressed.status_code == 200
    body = suppressed.json()
    assert body["status"] == "suppressed"
    assert body["last_status_action"] == "set_status:suppressed"
    assert body["last_status_acted_by"] == "owner"
    assert body["last_status_action_source"] == "dashboard_review"
    assert body["last_status_action_reason"] == "Not applicable for now"
    assert body["last_status_action_at"] is not None


@pytest.mark.asyncio
async def test_review_rule_candidate_endpoint_updates_status(client):
    from app.services.rule_lifecycle_service import get_rule_lifecycle_store

    store = get_rule_lifecycle_store()
    candidate = store.create_candidate(
        {
            "project": "alpha",
            "scope": "project",
            "topic_path": "rules/review",
            "marker_kind": "rule_project_candidate",
            "statement": "Agents should reject duplicate rule candidates.",
            "rationale": "This keeps the governed law pool compact.",
            "evidence_refs": ["test"],
            "source_task_id": "task-1",
            "source_session_id": "sess-1",
            "source_span_id": "span-review-endpoint",
            "source_work_id": "work-1",
            "confidence": 0.8,
            "promotion_hint": "",
            "related_rule_hint": None,
            "status": "candidate",
        }
    )

    response = await client.post(
        f"{PREFIX}/laws/candidates/{candidate.candidate_id}/review",
        json={
            "action": "needs_clarification",
            "reason": "Statement is too broad.",
            "acted_by": "codex",
            "source": "test",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["previous_status"] == "candidate"
    assert data["new_status"] == "needs_clarification"
    assert data["candidate"]["last_review_action"] == "needs_clarification"
    assert data["candidate"]["last_review_reason"] == "Statement is too broad."


@pytest.mark.asyncio
async def test_create_rule_candidate_endpoint_supports_trial_status(client):
    response = await client.post(
        f"{PREFIX}/laws/candidates",
        json={
            "project": "alpha",
            "title": "Frontend Backend Separation",
            "statement": "Frontend code must communicate with backend services through IPC.",
            "rationale": "Keeps clients independent from service internals.",
            "evidence_refs": ["test:evidence"],
            "status": "trial",
            "review_after_days": 0,
            "trial_days": 14,
            "acted_by": "codex",
        },
    )

    assert response.status_code == 201, response.text
    data = response.json()
    assert data["project"] == "alpha"
    assert data["status"] == "trial"
    assert data["statement"] == "Frontend code must communicate with backend services through IPC."
    assert data["marker_kind"] == "rule_project_candidate"
    assert data["source_span_id"].startswith("direct-rule:")
    assert data["trial_started_at"] is not None
    assert data["trial_review_after"] is not None
    assert data["trial_expires_at"] is not None

    due = await client.get(f"{PREFIX}/laws/candidates?project=alpha&status=trial&review_due=true")
    assert due.status_code == 200, due.text
    due_items = due.json()["items"]
    assert any(item["candidate_id"] == data["candidate_id"] for item in due_items)


@pytest.mark.asyncio
async def test_promote_rule_candidate_endpoint_creates_law_and_records_trace(client):
    from app.services.rule_lifecycle_service import get_rule_lifecycle_store

    store = get_rule_lifecycle_store()
    candidate = store.create_candidate(
        {
            "project": "alpha",
            "scope": "canonical_candidate",
            "topic_path": "agent/task-framing/clarify-before-implementation",
            "marker_kind": "rule_canonical_candidate",
            "statement": "Agents must clarify task framing before implementation.",
            "rationale": "This avoids implementing unresolved requirements.",
            "evidence_refs": ["test:evidence"],
            "source_task_id": "task-1",
            "source_session_id": "sess-1",
            "source_span_id": "span-promote-endpoint",
            "source_work_id": "work-1",
            "confidence": 0.9,
            "promotion_hint": "",
            "related_rule_hint": None,
            "status": "candidate",
        }
    )

    response = await client.post(
        f"{PREFIX}/laws/candidates/{candidate.candidate_id}/promote",
        json={
            "title": "Clarify Task Framing Before Implementation",
            "target_scope": "principle",
            "status": "proposed",
            "reason": "Promote after review packet found no duplicate.",
            "acted_by": "codex",
            "source": "test",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["previous_status"] == "candidate"
    assert data["new_status"] == "suppressed"
    assert data["law"]["title"] == "Clarify Task Framing Before Implementation"
    assert data["law"]["scope"] == "principle"
    assert data["law"]["status"] == "proposed"
    assert data["candidate"]["promoted_law_id"] == data["law"]["id"]
    assert data["candidate"]["last_review_action"] == "promote"
    assert data["candidate"]["promoted_at"] is not None


@pytest.mark.asyncio
async def test_promote_rule_candidate_endpoint_is_idempotent_for_same_candidate(client):
    from app.services.rule_lifecycle_service import get_rule_lifecycle_store

    store = get_rule_lifecycle_store()
    candidate = store.create_candidate(
        {
            "project": "alpha",
            "scope": "project",
            "topic_path": "agent/mailbox",
            "marker_kind": "rule_project_candidate",
            "statement": "Agents should use public mailbox forms for simple workflows.",
            "rationale": "This keeps weak-model routing stable.",
            "evidence_refs": ["test:evidence"],
            "source_task_id": "task-1",
            "source_session_id": "sess-1",
            "source_span_id": "span-promote-idempotent",
            "source_work_id": "work-1",
            "confidence": 0.9,
            "promotion_hint": "",
            "related_rule_hint": None,
            "status": "candidate",
        }
    )

    payload = {
        "title": "Use Public Mailbox Forms",
        "target_scope": "project",
        "status": "proposed",
        "reason": "Promote after review.",
        "acted_by": "codex",
        "source": "test",
    }
    first = await client.post(f"{PREFIX}/laws/candidates/{candidate.candidate_id}/promote", json=payload)
    assert first.status_code == 200, first.text
    first_body = first.json()

    second = await client.post(f"{PREFIX}/laws/candidates/{candidate.candidate_id}/promote", json=payload)
    assert second.status_code == 200, second.text
    second_body = second.json()

    assert second_body["law"]["id"] == first_body["law"]["id"]
    listed = await client.get(f"{PREFIX}/laws?project=alpha&status=all")
    assert listed.status_code == 200
    matching = [
        item
        for item in listed.json()["items"]
        if f"rule_candidate:{candidate.candidate_id}" in item["tags"]
    ]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_revise_law_from_rule_candidate_endpoint_creates_pending_revision(client):
    from app.services.rule_lifecycle_service import get_rule_lifecycle_store

    created_law = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Clarify Tasks",
        "statement": "Agents should clarify tasks.",
        "rationale": "Avoids confusion.",
        "agent_id": "codex",
        "scope": "project",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created_law.status_code == 201, created_law.text
    law_id = created_law.json()["id"]

    store = get_rule_lifecycle_store()
    candidate = store.create_candidate(
        {
            "project": "alpha",
            "scope": "project",
            "topic_path": "agent/task-framing",
            "marker_kind": "rule_project_candidate",
            "statement": "Agents must clarify task framing before implementation and present best options.",
            "rationale": "This avoids implementing unresolved requirements.",
            "evidence_refs": ["test:evidence"],
            "source_task_id": "task-1",
            "source_session_id": "sess-1",
            "source_span_id": "span-revise-law-endpoint",
            "source_work_id": "work-1",
            "confidence": 0.9,
            "promotion_hint": "",
            "related_rule_hint": law_id,
            "status": "candidate",
        }
    )

    response = await client.post(
        f"{PREFIX}/laws/candidates/{candidate.candidate_id}/revise-law",
        json={
            "law_id": law_id,
            "reason": "Candidate improves the existing law.",
            "acted_by": "codex",
            "source": "test",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["previous_status"] == "candidate"
    assert data["new_status"] == "revision_pending"
    assert data["candidate"]["revised_law_id"] == law_id
    assert data["candidate"]["last_review_action"] == "revise_existing_law"
    assert data["law"]["status"] == "active"
    assert data["law"]["statement"] == "Agents should clarify tasks."
    assert data["law"]["candidate_revision"]["statement"] == (
        "Agents must clarify task framing before implementation and present best options."
    )


@pytest.mark.asyncio
async def test_material_revision_creates_candidate_while_active_law_remains_effective(client):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Document risky changes",
        "statement": "Agents must document risky changes.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created.status_code == 201
    law_id = created.json()["id"]

    updated = await client.patch(f"{PREFIX}/laws/{law_id}", json={
        "statement": "Agents must document risky changes with rationale and rollback notes.",
    })
    assert updated.status_code == 200
    body = updated.json()
    assert body["status"] == "active"
    assert body["statement"] == "Agents must document risky changes."
    assert body["confirmed_by"] == "user"
    assert body["confirmed_at"] is not None
    assert body["candidate_revision"]["statement"] == "Agents must document risky changes with rationale and rollback notes."
    assert "pending" in body["status_reason"].lower()

    confirmed = await client.post(f"{PREFIX}/laws/{law_id}/confirm", json={
        "confirmed_by": "user",
        "confirmation_source": "inline_user_approval",
        "reason": "approve the revised law",
        "activate": True,
    })
    assert confirmed.status_code == 200
    confirmed_body = confirmed.json()
    assert confirmed_body["status"] == "active"
    assert confirmed_body["statement"] == "Agents must document risky changes with rationale and rollback notes."
    assert confirmed_body["candidate_revision"] is None


@pytest.mark.asyncio
async def test_create_active_law_without_confirmation_is_rejected(client):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Unsafe activation",
        "statement": "This should not become active silently.",
        "agent_id": "codex",
        "status": "active",
    })
    assert created.status_code == 400
    assert "require explicit confirmation" in created.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_confirmed_law_uses_deterministic_vector_when_embedding_unavailable(client):
    from app import dependencies
    from app.config import settings

    dependencies.get_ollama().embed.side_effect = RuntimeError("embedding provider unavailable")

    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Confirmed deterministic law",
        "statement": "Confirmed user laws should not be blocked by an unavailable embedding provider.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
        "confirmation_source": "test",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "active"
    assert body["confirmed_by"] == "user"
    fetched = await client.get(f"{PREFIX}/laws/{body['id']}")
    assert fetched.status_code == 200
    fetched_body = fetched.json()
    assert fetched_body["status"] == "active"
    record = await dependencies.get_qdrant().get(body["id"])
    assert record.meta["embedding_fallback"] == "zero_vector"
    assert len([0.0] * settings.embedding_dimensions) == settings.embedding_dimensions


@pytest.mark.asyncio
async def test_create_unconfirmed_law_uses_embedding_fallback_when_provider_unavailable(client):
    from app import dependencies

    dependencies.get_ollama().embed.side_effect = RuntimeError("embedding provider unavailable")
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Unconfirmed law",
        "statement": "Unconfirmed laws still require normal semantic storage.",
        "agent_id": "codex",
        "status": "proposed",
    })
    assert created.status_code == 201
    data = created.json()
    assert data["title"] == "Unconfirmed law"


@pytest.mark.asyncio
async def test_import_project_laws_from_markdown_is_repeatable(client, tmp_path):
    path = tmp_path / "PROJECT_LAW.md"
    path.write_text(
        "# Project Law\n\n"
        "## Law 1: Memory First\n\n"
        "Agents must start from project memory before reading code.\n\n"
        "Use project context before grep.\n\n"
        "## Law 2: User Sovereignty\n\n"
        "Only explicit user approval may activate project truth.\n",
        encoding="utf-8",
    )

    first = await client.post(f"{PREFIX}/laws/import-markdown", json={
        "project": "alpha",
        "path": str(path),
        "confirmed_by": "user",
        "confirmation_source": "inline_user_approval",
        "tags": ["migration"],
    })
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["parsed"] == 2
    assert first_body["created"] == 2
    assert first_body["skipped_existing"] == 0

    second = await client.post(f"{PREFIX}/laws/import-markdown", json={
        "project": "alpha",
        "path": str(path),
        "confirmed_by": "user",
        "confirmation_source": "inline_user_approval",
        "tags": ["migration"],
    })
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["parsed"] == 2
    assert second_body["created"] == 0
    assert second_body["skipped_existing"] == 2

    listed = await client.get(f"{PREFIX}/laws?project=alpha&status=active")
    assert listed.status_code == 200
    data = listed.json()
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_rule_candidate_projection_endpoint_uses_stenographer_markers(client, monkeypatch):
    stenographer = StenographerStore(Path(":memory:"))
    lifecycle = RuleLifecycleStore(Path(":memory:"))
    monkeypatch.setattr(stenographer_service, "_STORE", stenographer)
    monkeypatch.setattr(rule_lifecycle_service, "_STORE", lifecycle)
    try:
        stenographer.start_work_session(
            project="alpha",
            task_id="task-rule",
            agent_id="codex",
            session_id="sess-rule",
            work_id="work-rule",
        )
        stenographer.record_span(
            project="alpha",
            task_id="task-rule",
            agent_id="codex",
            session_id="sess-rule",
            kind="rule_project_candidate",
            source="reasoning_marker",
            content=json.dumps({
                "statement": "Alpha tests must use the declared Docker test contour.",
                "rationale": "This keeps test storage isolated from live data.",
                "topic_path": "testing/contour",
                "confidence": 0.85,
            }),
        )

        projected = await client.post(f"{PREFIX}/laws/candidates/project-from-stenography", json={"project": "alpha"})
        assert projected.status_code == 200, projected.text
        body = projected.json()
        assert body["created_candidates"] == 1
        assert body["candidates"][0]["status"] == "candidate"

        listed = await client.get(f"{PREFIX}/laws/candidates?project=alpha")
        assert listed.status_code == 200
        listed_body = listed.json()
        assert listed_body["total"] == 1
        assert listed_body["items"][0]["statement"] == "Alpha tests must use the declared Docker test contour."
    finally:
        stenographer.close()
        lifecycle.close()
        monkeypatch.setattr(stenographer_service, "_STORE", None)
        monkeypatch.setattr(rule_lifecycle_service, "_STORE", None)
