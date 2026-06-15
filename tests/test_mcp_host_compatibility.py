from pathlib import Path

from app.services.mcp_host_compatibility import (
    McpHostCompatibilityStore,
    resolve_task_continuity_scope,
)
from app.services.task_lease_service import TaskLeaseStore


def test_session_churn_is_derived_from_behavior_and_persists_in_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "compatibility.db"
    store = McpHostCompatibilityStore(db_path)
    try:
        first = store.observe(agent_fingerprint="agent-a", session_id="session-1", now=100.0)
        second = store.observe(agent_fingerprint="agent-a", session_id="session-2", now=110.0)
    finally:
        store.close()

    reopened = McpHostCompatibilityStore(db_path)
    try:
        third = reopened.observe(agent_fingerprint="agent-a", session_id="session-3", now=120.0)
    finally:
        reopened.close()

    assert first["session_behavior"] == "stable_or_unknown"
    assert second["session_behavior"] == "stateless_or_one_shot"
    assert third["session_behavior"] == "stateless_or_one_shot"
    assert "session_churn" in third["traits"]
    assert "agent-a" not in third["identity_key"]


def test_cooldown_is_isolated_by_safe_identity_scope(tmp_path: Path) -> None:
    store = McpHostCompatibilityStore(tmp_path / "compatibility.db")
    try:
        assert store.check_cooldown(scope_key="agent:a", event_key="cue:1", now=100.0) is False
        assert store.check_cooldown(scope_key="agent:a", event_key="cue:1", now=101.0) is True
        assert store.check_cooldown(scope_key="agent:b", event_key="cue:1", now=101.0) is False
    finally:
        store.close()


def test_missing_safe_identity_does_not_create_shared_profile(tmp_path: Path) -> None:
    store = McpHostCompatibilityStore(tmp_path / "compatibility.db")
    try:
        profile = store.observe(agent_fingerprint="", session_id="session-1", now=100.0)
    finally:
        store.close()

    assert profile["session_behavior"] == "unknown"
    assert "identity_key" not in profile


def test_task_continuity_requires_valid_active_lease(monkeypatch, tmp_path: Path) -> None:
    from app.services import task_lease_service as lease_mod

    lease_store = TaskLeaseStore(tmp_path / "leases.db")
    monkeypatch.setattr(lease_mod, "_STORE", lease_store)
    claim = lease_store.claim(
        project="alpha",
        task_id="task-1",
        owner_agent="codex",
        session_id="canonical-session",
        agent_fingerprint="agent-a",
    )
    try:
        active = resolve_task_continuity_scope(
            project="alpha",
            task_id="task-1",
            work_token=claim.work_token,
        )
        invalid = resolve_task_continuity_scope(
            project="alpha",
            task_id="task-1",
            work_token="wrong-token",
        )
        lease_store.release(
            lease_id=claim.lease.lease_id,
            owner_agent="codex",
            session_id="canonical-session",
        )
        released = resolve_task_continuity_scope(
            project="alpha",
            task_id="task-1",
            work_token=claim.work_token,
        )
    finally:
        lease_store.close()

    assert active["session_scope"] == "canonical-session"
    assert claim.work_token not in active["scope_key"]
    assert invalid == {}
    assert released == {}
