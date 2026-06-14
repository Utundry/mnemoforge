from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.autonomous_mode_service import AutonomousModeStore, evaluate_autonomous_mode


def _grant(*, expires_at: str) -> dict:
    return {
        "mode": "explicit_autonomous_mode",
        "approval_intent": "explicit_autonomous_mode",
        "approval_ref": "operator:approved-bundle-1",
        "approved_task_ids": ["task-1", "task-2"],
        "task_framing_versions": {"task-1": "v1", "task-2": "v3"},
        "allowed_actions": ["start_task", "record_progress", "finish_task"],
        "expires_at": expires_at,
        "permissions": {"commit": False, "live_mutation": False},
    }


def test_autonomous_mode_is_deny_by_default_and_task_list_is_exhaustive() -> None:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    allowed = evaluate_autonomous_mode(
        _grant(expires_at=future),
        task_id="task-1",
        action="start_task",
        framing_version="v1",
    )
    denied = evaluate_autonomous_mode(
        _grant(expires_at=future),
        task_id="linked-but-unapproved",
        action="start_task",
        framing_version="v1",
    )

    assert allowed["authority_granted"] is True
    assert allowed["task_relations_grant_authority"] is False
    assert denied["authority_granted"] is False
    assert denied["stop_reason"] == "unauthorized_task"
    assert denied["diagnostic_incident"]["kind"] == "autonomous_mode_denied"


def test_framing_change_expiry_and_separate_permissions_stop_mode() -> None:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    changed = evaluate_autonomous_mode(
        _grant(expires_at=future),
        task_id="task-1",
        action="start_task",
        framing_version="v2",
    )
    expired = evaluate_autonomous_mode(
        _grant(expires_at=past),
        task_id="task-1",
        action="start_task",
        framing_version="v1",
    )
    commit = evaluate_autonomous_mode(
        _grant(expires_at=future),
        task_id="task-1",
        action="commit",
        framing_version="v1",
    )

    assert changed["stop_reason"] == "framing_version_changed"
    assert expired["stop_reason"] == "expired"
    assert commit["stop_reason"] == "separate_permission_required"


def test_sqlite_store_is_source_of_truth_for_session_grant(tmp_path: Path) -> None:
    store = AutonomousModeStore(tmp_path / "autonomous.db")
    grant = _grant(expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    try:
        store.save(session_id="session-1", project="alpha", grant=grant)
        assert store.get(session_id="session-1", project="alpha")["approved_task_ids"] == ["task-1", "task-2"]

        store.revoke(session_id="session-1", project="alpha")
        assert store.get(session_id="session-1", project="alpha")["revoked"] is True
    finally:
        store.close()
