from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import task_lease_service as lease_mod
from app.services.mcp_task_lease_actions import (
    TaskLeaseActionDependencies,
    execute_task_lease_action,
    task_mutation_requires_owned_claim,
)
from app.services.mcp_task_session_actions import (
    TaskSessionActionDependencies,
    finish_task_session_action,
    start_task_session_action,
)
from app.services.mcp_work_session_actions import (
    WorkSessionActionDependencies,
    execute_work_session_action,
)
from app.services.mcp_checkpoint_draft_actions import (
    CheckpointDraftActionDependencies,
    execute_checkpoint_draft_action,
)
from app.services.mcp_task_checkpoint_actions import (
    TaskCheckpointActionDependencies,
    execute_task_checkpoint_action,
)
from app.services.task_lease_service import TaskLeaseConflict, TaskLeaseStore, TaskLeaseUnavailable, acquire_task_lease_with_heartbeat


def _now() -> datetime:
    return datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


async def test_mcp_task_lease_action_claim_uses_session_identity_defaults(monkeypatch) -> None:
    store = TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", store)

    async def fake_identity_defaults(session_id: str | None) -> dict[str, str]:
        assert session_id == "sess-codex"
        return {
            "agent_fingerprint": "agentfp:test",
            "runtime_profile_id": "strong_mcp_operator",
        }

    try:
        data = await execute_task_lease_action(
            name="claim_task",
            args={"project": "alpha", "task_id": "task-1", "owner_agent": "codex"},
            session_id="sess-codex",
            dependencies=TaskLeaseActionDependencies(get_session_identity_defaults=fake_identity_defaults),
        )
    finally:
        store.close()

    assert data["status"] == "claimed"
    assert data["lease"]["agent_fingerprint"] == "agentfp:test"
    assert data["lease"]["runtime_profile_id"] == "strong_mcp_operator"
    assert data["work_token"]
    assert data["work_handle"].startswith("wh1.")
    assert lease_mod.parse_work_handle(data["work_handle"])["task_id"] == "task-1"


def test_mcp_task_mutation_guard_accepts_valid_work_token(monkeypatch) -> None:
    store = TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", store)
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
        )
        guard = task_mutation_requires_owned_claim(
            project="alpha",
            task_id="task-1",
            owner_agent="other",
            owner_session_id="lost-session",
            tool_name="record_task_checkpoint",
            work_token=claim.work_token,
        )
    finally:
        store.close()

    assert guard is None


def test_mcp_task_mutation_guard_accepts_valid_work_handle(monkeypatch) -> None:
    store = TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", store)
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-handle",
            owner_agent="codex",
            session_id="sess-handle",
        )
        work_handle = lease_mod.build_work_handle(lease=claim.lease, work_token=claim.work_token)
        guard = task_mutation_requires_owned_claim(
            project="alpha",
            task_id="task-handle",
            owner_agent="codex",
            owner_session_id="",
            tool_name="record_task_checkpoint",
            work_handle=work_handle,
        )
    finally:
        store.close()

    assert guard is None


def test_mcp_task_mutation_guard_rejects_foreign_or_tampered_work_handle(monkeypatch) -> None:
    store = TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", store)
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-handle-block",
            owner_agent="codex",
            session_id="sess-handle",
        )
        work_handle = lease_mod.build_work_handle(lease=claim.lease, work_token=claim.work_token)
        foreign = task_mutation_requires_owned_claim(
            project="alpha",
            task_id="task-handle-block",
            owner_agent="other",
            owner_session_id="",
            tool_name="record_task_checkpoint",
            work_handle=work_handle,
        )
        replacement = "A" if work_handle[-1] != "A" else "B"
        tampered = task_mutation_requires_owned_claim(
            project="alpha",
            task_id="task-handle-block",
            owner_agent="codex",
            owner_session_id="",
            tool_name="record_task_checkpoint",
            work_handle=f"{work_handle[:-1]}{replacement}",
        )
    finally:
        store.close()

    assert foreign is not None
    assert foreign["error"] == "work_handle_owner_mismatch"
    assert tampered is not None
    assert tampered["error"] == "work_handle_signature_invalid"

def test_mcp_task_mutation_guard_accepts_same_owner_continuity_after_expired_claim(monkeypatch) -> None:
    store = TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", store)
    now = _now()
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-continuity",
            owner_agent="codex",
            session_id="sess-continuity",
            lease_ttl_seconds=5,
            now=now,
        )
        store.expire_stale(now=now + timedelta(seconds=10))
        guard = task_mutation_requires_owned_claim(
            project="alpha",
            task_id="task-continuity",
            owner_agent="codex",
            owner_session_id="sess-continuity",
            tool_name="record_task_checkpoint",
            work_token=claim.work_token,
        )
    finally:
        store.close()

    assert guard is None


def test_mcp_task_mutation_guard_blocks_active_other_owner_even_with_old_token(monkeypatch) -> None:
    store = TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", store)
    now = datetime.now(timezone.utc)
    try:
        first = store.claim(
            project="alpha",
            task_id="task-takeover",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=5,
            now=now,
        )
        store.expire_stale(now=now + timedelta(seconds=10))
        store.claim(
            project="alpha",
            task_id="task-takeover",
            owner_agent="other",
            session_id="sess-other",
            now=now + timedelta(seconds=11),
        )
        guard = task_mutation_requires_owned_claim(
            project="alpha",
            task_id="task-takeover",
            owner_agent="codex",
            owner_session_id="sess-codex",
            tool_name="record_task_checkpoint",
            work_token=first.work_token,
        )
    finally:
        store.close()

    assert guard is not None
    assert guard["error"] == "lease_owner_mismatch"


async def test_mcp_start_task_session_action_starts_work_and_checkpoint(monkeypatch) -> None:
    from app.services import stenographer_service as stenographer_mod

    lease_store = TaskLeaseStore(Path(":memory:"))
    stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", lease_store)
    monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
    posted: list[tuple[str, dict]] = []

    async def fake_post(api_base: str, path: str, payload: dict) -> dict:
        posted.append((path, payload))
        return {"id": "checkpoint-start-1", **payload}

    async def fake_identity_defaults(session_id: str | None) -> dict[str, str]:
        return {"agent_fingerprint": "agentfp:start", "runtime_profile_id": "strong_mcp_operator"}

    try:
        data = await start_task_session_action(
            args={
                "project": "alpha",
                "task_id": "task-start",
                "agent_id": "codex",
                "summary": "Starting from service.",
                "auto_heartbeat": False,
            },
            api_base="http://test",
            session_id="sess-start",
            dependencies=TaskSessionActionDependencies(
                post=fake_post,
                get_session_identity_defaults=fake_identity_defaults,
            ),
        )
    finally:
        lease_store.close()
        stenographer_store.close()

    assert data["status"] == "started"
    assert data["owner_session_id"] == "sess-start"
    assert data["lease"]["agent_fingerprint"] == "agentfp:start"
    assert data["lease"]["runtime_profile_id"] == "strong_mcp_operator"
    assert data["work_session"]["status"] == "active"
    assert data["checkpoint"]["id"] == "checkpoint-start-1"
    assert posted[0][0] == "/project/tasks/task-start/changes"


async def test_mcp_finish_task_session_action_finishes_work_and_releases(monkeypatch) -> None:
    from app.services import stenographer_service as stenographer_mod

    lease_store = TaskLeaseStore(Path(":memory:"))
    stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", lease_store)
    monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
    claim = lease_store.claim(
        project="alpha",
        task_id="task-finish",
        owner_agent="codex",
        session_id="sess-finish",
    )
    work = stenographer_store.start_work_session(
        project="alpha",
        task_id="task-finish",
        agent_id="codex",
        session_id="sess-finish",
        summary="Active work.",
    )
    posted: list[tuple[str, dict]] = []

    async def fake_post(api_base: str, path: str, payload: dict) -> dict:
        posted.append((path, payload))
        if path.endswith("/changes"):
            return {"id": "checkpoint-finish-1", **payload}
        return {"status": "ok"}

    async def fake_identity_defaults(session_id: str | None) -> dict[str, str]:
        return {}

    try:
        data = await finish_task_session_action(
            args={
                "project": "alpha",
                "task_id": "task-finish",
                "agent_id": "codex",
                "session_id": "sess-finish",
                "work_id": work.work_id,
                "work_token": claim.work_token,
                "summary": "Finished from service.",
                "changed_files": ["app/services/mcp_task_session_actions.py"],
                "verification": ["Docker contour passed."],
                "next_step": "No follow-up.",
            },
            api_base="http://test",
            session_id="sess-finish",
            dependencies=TaskSessionActionDependencies(
                post=fake_post,
                get_session_identity_defaults=fake_identity_defaults,
            ),
        )
    finally:
        lease_store.close()
        stenographer_store.close()

    assert data["status"] == "finished"
    assert data["release"]["status"] == "released"
    assert data["work_session"]["status"] == "completed"
    assert posted[0][0] == "/project/tasks/task-finish/changes"


async def test_mcp_finish_task_session_accepts_work_handle_without_work_id_or_session(monkeypatch) -> None:
    from app.services import stenographer_service as stenographer_mod

    lease_store = TaskLeaseStore(Path(":memory:"))
    stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", lease_store)
    monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
    claim = lease_store.claim(
        project="alpha",
        task_id="task-finish-handle",
        owner_agent="codex",
        session_id="sess-finish-handle",
    )
    stenographer_store.start_work_session(
        project="alpha",
        task_id="task-finish-handle",
        agent_id="codex",
        session_id="sess-finish-handle",
        summary="Active work via handle.",
    )
    work_handle = lease_mod.build_work_handle(lease=claim.lease, work_token=claim.work_token)
    posted: list[tuple[str, dict]] = []

    async def fake_post(api_base: str, path: str, payload: dict) -> dict:
        posted.append((path, payload))
        if path.endswith("/changes"):
            return {"id": "checkpoint-finish-handle", **payload}
        return {"status": "ok"}

    async def fake_identity_defaults(session_id: str | None) -> dict[str, str]:
        return {}

    try:
        data = await finish_task_session_action(
            args={
                "project": "alpha",
                "task_id": "task-finish-handle",
                "agent_id": "codex",
                "work_handle": work_handle,
                "summary": "Finished through public handle.",
                "changed_files": ["app/services/mcp_task_session_actions.py"],
                "verification": ["Handle-only finish passed."],
                "next_step": "No follow-up.",
            },
            api_base="http://test",
            session_id="lost-client-session",
            dependencies=TaskSessionActionDependencies(
                post=fake_post,
                get_session_identity_defaults=fake_identity_defaults,
            ),
        )
    finally:
        lease_store.close()
        stenographer_store.close()

    assert data["status"] == "finished"
    assert data["release"]["status"] == "released"
    assert data["work_session"]["status"] == "completed"
    assert posted[0][0] == "/project/tasks/task-finish-handle/changes"

async def test_mcp_finish_task_session_uses_same_owner_continuity_after_expired_claim(monkeypatch) -> None:
    from app.services import stenographer_service as stenographer_mod

    lease_store = TaskLeaseStore(Path(":memory:"))
    stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", lease_store)
    monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)
    now = _now()
    claim = lease_store.claim(
        project="alpha",
        task_id="task-finish-continuity",
        owner_agent="codex",
        session_id="sess-finish-continuity",
        lease_ttl_seconds=5,
        now=now,
    )
    work = stenographer_store.start_work_session(
        project="alpha",
        task_id="task-finish-continuity",
        agent_id="codex",
        session_id="sess-finish-continuity",
        summary="Active work survives lease expiry.",
    )
    lease_store.expire_stale(now=now + timedelta(seconds=10))
    posted: list[tuple[str, dict]] = []

    async def fake_post(api_base: str, path: str, payload: dict) -> dict:
        posted.append((path, payload))
        if path.endswith("/changes"):
            return {"id": "checkpoint-continuity-finish", **payload}
        return {"status": "ok"}

    async def fake_identity_defaults(session_id: str | None) -> dict[str, str]:
        return {}

    try:
        data = await finish_task_session_action(
            args={
                "project": "alpha",
                "task_id": "task-finish-continuity",
                "agent_id": "codex",
                "session_id": "sess-finish-continuity",
                "work_id": work.work_id,
                "work_token": claim.work_token,
                "summary": "Finished through continuity.",
                "changed_files": ["app/services/mcp_task_session_actions.py"],
                "verification": ["Docker contour passed."],
                "next_step": "No follow-up.",
            },
            api_base="http://test",
            session_id="sess-finish-continuity",
            dependencies=TaskSessionActionDependencies(
                post=fake_post,
                get_session_identity_defaults=fake_identity_defaults,
            ),
        )
    finally:
        lease_store.close()
        stenographer_store.close()

    assert data["status"] == "finished"
    assert data["continuity_reclaim"] is True
    assert data["release"]["status"] == "continuity_reclaim"
    assert data["release"]["lease"]["status"] == "expired"
    assert data["work_session"]["status"] == "completed"
    assert posted[0][0] == "/project/tasks/task-finish-continuity/changes"

def test_mcp_work_session_action_uses_mutation_guard(monkeypatch) -> None:
    from app.services import stenographer_service as stenographer_mod

    stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
    monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)

    def deny_guard(**kwargs):
        return {
            "status": "conflict",
            "error": "active_claim_required",
            "tool": kwargs["tool_name"],
        }

    try:
        data = execute_work_session_action(
            name="start_work_session",
            args={"project": "alpha", "task_id": "task-guard", "agent_id": "codex", "session_id": "sess-guard"},
            dependencies=WorkSessionActionDependencies(task_mutation_guard=deny_guard),
        )
    finally:
        stenographer_store.close()

    assert data["status"] == "conflict"
    assert data["tool"] == "start_work_session"


def test_mcp_work_session_action_records_and_lists_span(monkeypatch) -> None:
    from app.services import stenographer_service as stenographer_mod

    stenographer_store = stenographer_mod.StenographerStore(Path(":memory:"))
    monkeypatch.setattr(stenographer_mod, "_STORE", stenographer_store)

    def allow_guard(**kwargs):
        return None

    try:
        work = execute_work_session_action(
            name="start_work_session",
            args={
                "project": "alpha",
                "task_id": "task-span",
                "agent_id": "codex",
                "session_id": "sess-span",
                "summary": "Capture spans.",
            },
            dependencies=WorkSessionActionDependencies(task_mutation_guard=allow_guard),
        )
        span = execute_work_session_action(
            name="record_stenographer_span",
            args={
                "project": "alpha",
                "task_id": "task-span",
                "work_id": work["work_id"],
                "agent_id": "codex",
                "session_id": "sess-span",
                "kind": "verification",
                "source": "service-test",
                "content": "Docker contour passed.",
            },
            dependencies=WorkSessionActionDependencies(task_mutation_guard=allow_guard),
        )
        listed = execute_work_session_action(
            name="list_stenographer_spans",
            args={"project": "alpha", "task_id": "task-span", "work_id": work["work_id"]},
            dependencies=WorkSessionActionDependencies(task_mutation_guard=allow_guard),
        )
    finally:
        stenographer_store.close()

    assert span["work_id"] == work["work_id"]
    assert span["excluded_from_learning"] is True
    assert listed["total"] == 1
    assert listed["items"][0]["span_id"] == span["span_id"]


async def test_mcp_checkpoint_draft_action_returns_preview_view(monkeypatch) -> None:
    from uuid import uuid4
    from app.services import checkpoint_draft_service as draft_mod
    from app.services import stenographer_service as stenographer_mod

    root = Path("qdrant_data") / "test_mcp_checkpoint_draft_action"
    root.mkdir(parents=True, exist_ok=True)
    draft_store = draft_mod.CheckpointDraftStore(root / f"drafts-{uuid4().hex}.db")
    stenographer_store = stenographer_mod.StenographerStore(root / f"stenographer-{uuid4().hex}.db")
    monkeypatch.setattr(draft_mod, "_STORE", draft_store)
    monkeypatch.setattr(draft_mod, "get_stenographer_store", lambda: stenographer_store)

    try:
        stenographer_store.start_work_session(
            project="alpha",
            task_id="task-draft",
            agent_id="codex",
            session_id="sess-draft",
            work_id="work-draft",
        )
        for kind, content in (
            ("fact", "Implemented draft action service."),
            ("verification", "Docker contour passed."),
            ("changed_files", "app/services/mcp_checkpoint_draft_actions.py"),
            ("next_step", "Review the service extraction."),
        ):
            stenographer_store.record_span(
                project="alpha",
                task_id="task-draft",
                work_id="work-draft",
                agent_id="codex",
                session_id="sess-draft",
                kind=kind,
                source="service-test",
                content=content,
            )
        drafted = await execute_checkpoint_draft_action(
            name="draft_checkpoint_from_spans",
            args={
                "project": "alpha",
                "task_id": "task-draft",
                "work_id": "work-draft",
                "agent_id": "codex",
                "session_id": "sess-draft",
                "use_llm": False,
                "preserve_evidence": True,
            },
            dependencies=CheckpointDraftActionDependencies(llm_gateway=None, qdrant=None, ollama=None),
        )
        preview = await execute_checkpoint_draft_action(
            name="get_checkpoint_draft",
            args={"draft_id": drafted["draft_id"], "view": "preview"},
            dependencies=CheckpointDraftActionDependencies(llm_gateway=None, qdrant=None, ollama=None),
        )
    finally:
        stenographer_store.close()
        draft_store.close()

    assert drafted["mutates_memory"] is False
    assert drafted["recommended_next_tool"] == "approve_checkpoint_draft"
    assert preview["draft_id"] == drafted["draft_id"]
    assert "record_task_checkpoint_args" not in preview
    assert preview["recommended_next_tool"] == "approve_checkpoint_draft"


async def test_mcp_task_checkpoint_action_records_change_and_stage_evidence(monkeypatch) -> None:
    posted: list[tuple[str, dict]] = []

    async def fake_post(api_base: str, path: str, payload: dict) -> dict:
        posted.append((path, payload))
        return {"id": "change-service-1", "task_id": "task-checkpoint"}

    async def fake_get(api_base: str, path: str) -> dict:
        return {
            "task_id": "task-checkpoint",
            "title": "Checkpoint service extraction",
            "description": "Move checkpoint recording out of the SSE router.",
            "tags": ["checkpoint", "service"],
        }

    def allow_guard(**kwargs):
        return None

    result = await execute_task_checkpoint_action(
        name="report_task_checkpoint",
        args={
            "project": "alpha",
            "task_id": "task-checkpoint",
            "stage": "planning",
            "summary": "Checkpoint service extraction planned.",
            "checkpoint_mode": "lightweight",
            "acted_by": "codex",
        },
        api_base="http://test",
        dependencies=TaskCheckpointActionDependencies(
            post=fake_post,
            get=fake_get,
            task_mutation_guard=allow_guard,
        ),
    )

    assert posted[0][0] == "/project/tasks/task-checkpoint/changes"
    assert "Checkpoint recorded for task task-checkpoint" in result
    assert "stage_evidence=checkpoint:change-service-1" in result


def test_task_lease_blocks_second_agent_until_timeout() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        first = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )
        assert first.status == "claimed"
        assert first.lease.status == "active"

        with pytest.raises(TaskLeaseConflict) as exc_info:
            store.claim(
                project="alpha",
                task_id="task-1",
                owner_agent="claude",
                session_id="sess-claude",
                lease_ttl_seconds=30,
                now=_now() + timedelta(seconds=5),
            )
        assert exc_info.value.active_lease.lease_id == first.lease.lease_id
        assert exc_info.value.active_lease.owner_agent == "codex"
    finally:
        store.close()


def test_task_lease_store_migrates_old_database_before_fingerprint_index(tmp_path) -> None:
    db_path = tmp_path / "task_leases.db"
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE task_leases (
                lease_id            TEXT PRIMARY KEY,
                project             TEXT NOT NULL,
                task_id             TEXT NOT NULL,
                owner_agent         TEXT NOT NULL,
                session_id          TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL,
                claimed_at          REAL NOT NULL,
                heartbeat_at        REAL NOT NULL,
                expires_at          REAL NOT NULL,
                released_at         REAL,
                release_reason      TEXT NOT NULL DEFAULT '',
                lease_ttl_seconds   INTEGER NOT NULL,
                previous_lease_id   TEXT NOT NULL DEFAULT '',
                work_token_hash     TEXT NOT NULL DEFAULT '',
                work_token_preview  TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_task_leases_project_task_status
                ON task_leases(project, task_id, status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_task_leases_owner_session
                ON task_leases(owner_agent, session_id, status, heartbeat_at);
            """
        )
        con.commit()
    finally:
        con.close()

    store = TaskLeaseStore(db_path)
    try:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(task_leases)").fetchall()}
        indexes = {row[1] for row in store._conn.execute("PRAGMA index_list(task_leases)").fetchall()}

        assert "agent_fingerprint" in columns
        assert "runtime_profile_id" in columns
        assert "idx_task_leases_fingerprint" in indexes
    finally:
        store.close()


def test_task_lease_reentrant_claim_renews_same_owner() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        first = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )
        renewed = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=60,
            now=_now() + timedelta(seconds=10),
        )

        assert renewed.status == "renewed"
        assert renewed.lease.lease_id == first.lease.lease_id
        assert renewed.lease.expires_at == _now() + timedelta(seconds=70)
        assert renewed.work_token
        assert renewed.work_token != first.work_token
        assert store.verify_work_token(
            lease_id=renewed.lease.lease_id,
            work_token=renewed.work_token,
        )
        assert not store.verify_work_token(
            lease_id=renewed.lease.lease_id,
            work_token=first.work_token,
        )
    finally:
        store.close()


def test_task_lease_timeout_releases_zombie_claim_for_next_agent() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        first = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )

        second = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="claude",
            session_id="sess-claude",
            lease_ttl_seconds=30,
            now=_now() + timedelta(seconds=31),
        )

        assert second.status == "claimed"
        assert second.previous_claim_expired is True
        assert second.previous_lease is not None
        assert second.previous_lease.lease_id == first.lease.lease_id
        assert second.previous_lease.status == "expired"
        assert second.lease.previous_lease_id == first.lease.lease_id
        assert second.lease.owner_agent == "claude"
    finally:
        store.close()


def test_task_lease_records_agent_fingerprint_and_runtime_profile() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        result = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            agent_fingerprint="agentfp:codex",
            runtime_profile_id="weak_mcp_operator",
            lease_ttl_seconds=30,
            now=_now(),
        )

        assert result.lease.agent_fingerprint == "agentfp:codex"
        assert result.lease.runtime_profile_id == "weak_mcp_operator"

        by_fingerprint = store.list_leases(project="alpha", agent_fingerprint="agentfp:codex")
        assert [lease.lease_id for lease in by_fingerprint] == [result.lease.lease_id]

        by_profile = store.list_leases(project="alpha", runtime_profile_id="weak_mcp_operator")
        assert [lease.lease_id for lease in by_profile] == [result.lease.lease_id]
    finally:
        store.close()


def test_task_lease_same_fingerprint_reclaim_after_timeout() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        first = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex-a",
            agent_fingerprint="agentfp:same",
            runtime_profile_id="strong_mcp_operator",
            lease_ttl_seconds=30,
            now=_now(),
        )

        second = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex-b",
            agent_fingerprint="agentfp:same",
            runtime_profile_id="strong_mcp_operator",
            lease_ttl_seconds=30,
            now=_now() + timedelta(seconds=31),
        )

        assert second.status == "reclaimed"
        assert second.same_fingerprint_reclaim is True
        assert second.previous_claim_expired is True
        assert second.previous_lease is not None
        assert second.previous_lease.lease_id == first.lease.lease_id
        assert second.previous_lease.agent_fingerprint == "agentfp:same"
        assert second.lease.previous_lease_id == first.lease.lease_id
    finally:
        store.close()


def test_task_lease_different_fingerprint_after_timeout_is_new_claim() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        first = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            agent_fingerprint="agentfp:old",
            lease_ttl_seconds=30,
            now=_now(),
        )

        second = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="claude",
            session_id="sess-claude",
            agent_fingerprint="agentfp:new",
            lease_ttl_seconds=30,
            now=_now() + timedelta(seconds=31),
        )

        assert second.status == "claimed"
        assert second.same_fingerprint_reclaim is False
        assert second.previous_claim_expired is True
        assert second.previous_lease is not None
        assert second.previous_lease.lease_id == first.lease.lease_id
    finally:
        store.close()


def test_task_lease_previous_expired_claim_is_scoped_to_same_task() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        unrelated = store.claim(
            project="alpha",
            task_id="task-old",
            owner_agent="codex",
            session_id="sess-old",
            agent_fingerprint="agentfp:old",
            lease_ttl_seconds=30,
            now=_now(),
        )

        current = store.claim(
            project="alpha",
            task_id="task-new",
            owner_agent="codex",
            session_id="sess-new",
            agent_fingerprint="agentfp:new",
            lease_ttl_seconds=30,
            now=_now() + timedelta(seconds=31),
        )

        assert current.status == "claimed"
        assert current.previous_claim_expired is False
        assert current.previous_lease is None
        assert current.lease.previous_lease_id == ""
        assert unrelated.lease.lease_id != current.lease.lease_id
    finally:
        store.close()


def test_task_lease_same_fingerprint_and_work_token_reclaims_active_session() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        first = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-lost",
            agent_fingerprint="agentfp:same",
            runtime_profile_id="strong_mcp_operator",
            lease_ttl_seconds=30,
            now=_now(),
        )

        reclaimed = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-recovered",
            agent_fingerprint="agentfp:same",
            runtime_profile_id="strong_mcp_operator",
            work_token=first.work_token,
            lease_ttl_seconds=60,
            now=_now() + timedelta(seconds=10),
        )

        assert reclaimed.status == "reclaimed"
        assert reclaimed.same_fingerprint_reclaim is True
        assert reclaimed.lease.lease_id == first.lease.lease_id
        assert reclaimed.lease.session_id == "sess-recovered"
        assert reclaimed.lease.expires_at == _now() + timedelta(seconds=70)
        assert reclaimed.previous_lease is not None
        assert reclaimed.previous_lease.session_id == "sess-lost"
        assert reclaimed.work_token == first.work_token
    finally:
        store.close()


def test_task_lease_same_fingerprint_rejects_invalid_work_token() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        first = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-lost",
            agent_fingerprint="agentfp:same",
            lease_ttl_seconds=30,
            now=_now(),
        )

        with pytest.raises(PermissionError):
            store.claim(
                project="alpha",
                task_id="task-1",
                owner_agent="codex",
                session_id="sess-recovered",
                agent_fingerprint="agentfp:same",
                work_token="wrong-token",
                lease_ttl_seconds=60,
                now=_now() + timedelta(seconds=10),
            )

        active = store.get_active_claim(project="alpha", task_id="task-1", now=_now() + timedelta(seconds=11))
        assert active is not None
        assert active.lease_id == first.lease.lease_id
        assert active.session_id == "sess-lost"
    finally:
        store.close()


def test_task_lease_heartbeat_extends_expiration() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )
        refreshed = store.heartbeat(
            lease_id=claim.lease.lease_id,
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=90,
            now=_now() + timedelta(seconds=20),
        )

        assert refreshed.status == "active"
        assert refreshed.heartbeat_at == _now() + timedelta(seconds=20)
        assert refreshed.expires_at == _now() + timedelta(seconds=110)
        assert store.get_active_claim(project="alpha", task_id="task-1", now=_now() + timedelta(seconds=80)) is not None
    finally:
        store.close()


def test_task_lease_heartbeat_expired_claim_reports_structured_unavailable() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )

        with pytest.raises(TaskLeaseUnavailable) as exc_info:
            store.heartbeat(
                lease_id=claim.lease.lease_id,
                owner_agent="codex",
                session_id="sess-codex",
                now=_now() + timedelta(seconds=31),
            )

        assert exc_info.value.reason == "lease_expired"
        assert exc_info.value.lease.status == "expired"
        assert exc_info.value.to_dict()["lease"]["lease_id"] == claim.lease.lease_id
    finally:
        store.close()


def test_task_lease_release_removes_active_claim() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )
        released = store.release(
            lease_id=claim.lease.lease_id,
            owner_agent="codex",
            session_id="sess-codex",
            reason="handoff",
            now=_now() + timedelta(seconds=10),
        )

        assert released.status == "released"
        assert released.release_reason == "handoff"
        assert store.get_active_claim(project="alpha", task_id="task-1", now=_now() + timedelta(seconds=11)) is None
    finally:
        store.close()


def test_task_lease_requires_matching_session_for_release_and_heartbeat() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-a",
            lease_ttl_seconds=30,
            now=_now(),
        )
        with pytest.raises(PermissionError):
            store.release(
                lease_id=claim.lease.lease_id,
                owner_agent="codex",
                session_id="sess-b",
                reason="wrong-session",
                now=_now() + timedelta(seconds=5),
            )
        with pytest.raises(PermissionError):
            store.heartbeat(
                lease_id=claim.lease.lease_id,
                owner_agent="codex",
                session_id="sess-b",
                now=_now() + timedelta(seconds=6),
            )
    finally:
        store.close()


def test_task_lease_force_release_with_audit_reason() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        claim = store.claim(
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )
        released = store.force_release(
            lease_id=claim.lease.lease_id,
            acted_by="operator",
            reason="owner-session-lost",
            status="released",
            now=_now() + timedelta(seconds=5),
        )
        assert released.status == "released"
        assert released.release_reason == "force_release:operator:owner-session-lost"
        assert store.get_active_claim(project="alpha", task_id="task-1", now=_now() + timedelta(seconds=6)) is None
    finally:
        store.close()


def test_task_lease_heartbeat_handle_renews_without_agent_call() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        claim, handle = acquire_task_lease_with_heartbeat(
            store=store,
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            heartbeat_seconds=10,
            now=_now(),
        )
        try:
            refreshed = handle.heartbeat_once(now=_now() + timedelta(seconds=20))
        finally:
            handle.close()

        assert claim.lease.lease_id == refreshed.lease_id
        assert refreshed.heartbeat_at == _now() + timedelta(seconds=20)
        assert refreshed.expires_at == _now() + timedelta(seconds=50)
        assert store.get_active_claim(project="alpha", task_id="task-1", now=_now() + timedelta(seconds=40)) is not None
    finally:
        store.close()


def test_task_lease_heartbeat_handle_can_release_on_close() -> None:
    store = TaskLeaseStore(Path(":memory:"))
    try:
        claim, handle = acquire_task_lease_with_heartbeat(
            store=store,
            project="alpha",
            task_id="task-1",
            owner_agent="codex",
            session_id="sess-codex",
            lease_ttl_seconds=30,
            now=_now(),
        )
        handle.close(release=True, reason="session_closed")

        leases = store.list_leases(project="alpha", task_id="task-1", status="all")
        assert leases[0].lease_id == claim.lease.lease_id
        assert leases[0].status == "released"
        assert leases[0].release_reason == "session_closed"
    finally:
        store.close()
