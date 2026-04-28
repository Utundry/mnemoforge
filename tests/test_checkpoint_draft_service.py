from pathlib import Path
from uuid import uuid4

import pytest

from app.services import checkpoint_draft_service
from app.services.checkpoint_draft_service import (
    CheckpointDraftStore,
    DraftValidationError,
    approve_checkpoint_draft,
    draft_checkpoint_from_spans,
    get_checkpoint_draft,
    reject_checkpoint_draft,
    revise_checkpoint_draft,
)
from app.services.stenographer_service import StenographerStore


def _db_path(name: str) -> Path:
    root = Path("qdrant_data") / "test_checkpoint_drafts"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{name}-{uuid4().hex}.db"


def _seed_spans(store: StenographerStore) -> None:
    store.start_work_session(
        project="alpha",
        task_id="task-1",
        agent_id="codex",
        session_id="sess-1",
        work_id="work-1",
    )
    store.record_span(
        project="alpha",
        task_id="task-1",
        agent_id="codex",
        session_id="sess-1",
        work_id="work-1",
        kind="fact",
        source="notes",
        content="Implemented checkpoint drafts from stenographer spans.",
    )
    store.record_span(
        project="alpha",
        task_id="task-1",
        agent_id="codex",
        session_id="sess-1",
        work_id="work-1",
        kind="decision",
        source="design",
        content="Approve drafts by reference instead of replaying checkpoint payloads.",
    )
    store.record_span(
        project="alpha",
        task_id="task-1",
        agent_id="codex",
        session_id="sess-1",
        work_id="work-1",
        kind="verification",
        source="pytest",
        content="pytest tests/test_checkpoint_draft_service.py passed",
    )
    store.record_span(
        project="alpha",
        task_id="task-1",
        agent_id="codex",
        session_id="sess-1",
        work_id="work-1",
        kind="next_step",
        source="plan",
        content="Expose checkpoint draft tools through MCP.",
    )


@pytest.mark.asyncio
async def test_checkpoint_draft_from_spans_is_review_only(monkeypatch) -> None:
    stenographer = StenographerStore(_db_path("stenographer"))
    drafts = CheckpointDraftStore(_db_path("drafts"))
    monkeypatch.setattr(checkpoint_draft_service, "get_stenographer_store", lambda: stenographer)
    try:
        _seed_spans(stenographer)

        draft = await draft_checkpoint_from_spans(
            {
                "project": "alpha",
                "task_id": "task-1",
                "work_id": "work-1",
                "agent_id": "codex",
                "session_id": "sess-1",
                "use_llm": False,
            },
            store=drafts,
        )

        assert draft.version == 1
        assert draft.status == "drafted"
        assert draft.validation_report["can_approve"] is True
        assert draft.source_span_ids
        assert draft.metrics["estimated_saved_chars"] > 0
        assert draft.record_task_checkpoint_args["task_id"] == "task-1"
        assert "Approve drafts by reference" in draft.record_task_checkpoint_args["decisions"][0]
        assert get_checkpoint_draft(draft.draft_id, store=drafts).preview == draft.preview
    finally:
        stenographer.close()
        drafts.close()


@pytest.mark.asyncio
async def test_checkpoint_draft_uses_changed_files_span(monkeypatch) -> None:
    stenographer = StenographerStore(_db_path("stenographer"))
    drafts = CheckpointDraftStore(_db_path("drafts"))
    monkeypatch.setattr(checkpoint_draft_service, "get_stenographer_store", lambda: stenographer)
    try:
        _seed_spans(stenographer)
        stenographer.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
            kind="changed_files",
            source="git",
            content="app/services/checkpoint_draft_service.py; tests/test_checkpoint_draft_service.py",
        )
        stenographer.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
            kind="next_step",
            source="closeout",
            content="Approve the checkpoint draft.",
        )

        draft = await draft_checkpoint_from_spans(
            {"project": "alpha", "task_id": "task-1", "work_id": "work-1", "use_llm": False},
            store=drafts,
        )

        assert draft.record_task_checkpoint_args["changed_files"] == [
            "app/services/checkpoint_draft_service.py",
            "tests/test_checkpoint_draft_service.py",
        ]
        assert draft.validation_report["can_approve"] is True
    finally:
        stenographer.close()
        drafts.close()


@pytest.mark.asyncio
async def test_checkpoint_draft_drops_stale_next_step_after_later_evidence(monkeypatch) -> None:
    stenographer = StenographerStore(_db_path("stenographer"))
    drafts = CheckpointDraftStore(_db_path("drafts"))
    monkeypatch.setattr(checkpoint_draft_service, "get_stenographer_store", lambda: stenographer)
    try:
        stenographer.start_work_session(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
        )
        stenographer.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
            kind="fact",
            source="notes",
            content="Implemented the generated symbol flow.",
        )
        stenographer.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
            kind="next_step",
            source="risk",
            content="Update namedparams.py.",
        )
        stenographer.record_span(
            project="alpha",
            task_id="task-1",
            agent_id="codex",
            session_id="sess-1",
            work_id="work-1",
            kind="verification",
            source="pytest",
            content="Updated namedparams.py and pytest tests/test_checkpoint_draft_service.py passed.",
        )

        draft = await draft_checkpoint_from_spans(
            {"project": "alpha", "task_id": "task-1", "work_id": "work-1", "use_llm": False},
            store=drafts,
        )

        assert draft.record_task_checkpoint_args["next_step"] == ""
        assert "next_step" in draft.validation_report["missing"]
        assert draft.validation_report["can_approve"] is False
    finally:
        stenographer.close()
        drafts.close()


@pytest.mark.asyncio
async def test_approve_checkpoint_draft_saves_by_reference(monkeypatch) -> None:
    stenographer = StenographerStore(_db_path("stenographer"))
    drafts = CheckpointDraftStore(_db_path("drafts"))
    monkeypatch.setattr(checkpoint_draft_service, "get_stenographer_store", lambda: stenographer)
    saved_payloads: list[dict] = []

    async def fake_save(payload: dict) -> dict:
        saved_payloads.append(payload)
        return {"id": "change-1"}

    try:
        _seed_spans(stenographer)
        draft = await draft_checkpoint_from_spans(
            {"project": "alpha", "task_id": "task-1", "work_id": "work-1", "use_llm": False},
            store=drafts,
        )

        approved = await approve_checkpoint_draft(
            draft.draft_id,
            draft.version,
            approved_by="codex",
            save_checkpoint=fake_save,
            store=drafts,
        )

        assert approved.status == "approved"
        assert approved.saved_change_id == "change-1"
        assert saved_payloads == [draft.record_task_checkpoint_args]
    finally:
        stenographer.close()
        drafts.close()


@pytest.mark.asyncio
async def test_revise_creates_new_version_and_stale_approve_is_blocked(monkeypatch) -> None:
    stenographer = StenographerStore(_db_path("stenographer"))
    drafts = CheckpointDraftStore(_db_path("drafts"))
    monkeypatch.setattr(checkpoint_draft_service, "get_stenographer_store", lambda: stenographer)

    async def fake_save(payload: dict) -> dict:
        return {"id": "change-2"}

    try:
        _seed_spans(stenographer)
        draft = await draft_checkpoint_from_spans(
            {"project": "alpha", "task_id": "task-1", "work_id": "work-1", "use_llm": False},
            store=drafts,
        )
        revised = revise_checkpoint_draft(
            draft.draft_id,
            {"summary": "Implemented approve-by-reference checkpoint drafts."},
            store=drafts,
        )

        assert revised.version == 2
        assert revised.content_hash != draft.content_hash

        with pytest.raises(DraftValidationError) as exc_info:
            await approve_checkpoint_draft(
                draft.draft_id,
                1,
                save_checkpoint=fake_save,
                store=drafts,
            )
        assert exc_info.value.code == "stale_draft_version"

        approved = await approve_checkpoint_draft(
            draft.draft_id,
            revised.version,
            save_checkpoint=fake_save,
            store=drafts,
        )
        assert approved.status == "approved"
    finally:
        stenographer.close()
        drafts.close()


@pytest.mark.asyncio
async def test_reject_blocks_later_approval(monkeypatch) -> None:
    stenographer = StenographerStore(_db_path("stenographer"))
    drafts = CheckpointDraftStore(_db_path("drafts"))
    monkeypatch.setattr(checkpoint_draft_service, "get_stenographer_store", lambda: stenographer)

    async def fake_save(payload: dict) -> dict:
        return {"id": "change-3"}

    try:
        _seed_spans(stenographer)
        draft = await draft_checkpoint_from_spans(
            {"project": "alpha", "task_id": "task-1", "work_id": "work-1", "use_llm": False},
            store=drafts,
        )
        rejected = reject_checkpoint_draft(draft.draft_id, draft.version, reason="Needs operator review.", store=drafts)
        assert rejected.status == "rejected"

        with pytest.raises(DraftValidationError) as exc_info:
            await approve_checkpoint_draft(
                draft.draft_id,
                draft.version,
                save_checkpoint=fake_save,
                store=drafts,
            )
        assert exc_info.value.code == "draft_not_approvable"
    finally:
        stenographer.close()
        drafts.close()
