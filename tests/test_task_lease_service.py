from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.task_lease_service import TaskLeaseConflict, TaskLeaseStore, TaskLeaseUnavailable, acquire_task_lease_with_heartbeat


def _now() -> datetime:
    return datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


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
