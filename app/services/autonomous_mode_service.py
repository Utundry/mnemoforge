from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec
from app.services.public_diagnostic_service import build_public_diagnostic_incident
from app.services.system_data_root import data_path


_DB_PATH = data_path("autonomous_mode.db")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS autonomous_mode_grants (
    session_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    mode TEXT NOT NULL,
    grant_json TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


@lru_cache(maxsize=1)
def _spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/autonomous_mode.json")
    except Exception:
        return {
            "default_mode": "collaborative_control",
            "autonomous_mode": "explicit_autonomous_mode",
            "approval_intent": "explicit_autonomous_mode",
            "allowed_actions": [],
            "separate_permissions": ["commit", "live_mutation"],
        }


class AutonomousModeStore:
    def __init__(self, path: Path = _DB_PATH) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def save(self, *, session_id: str, project: str, grant: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO autonomous_mode_grants(session_id, project, mode, grant_json, revoked, updated_at)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                project=excluded.project,
                mode=excluded.mode,
                grant_json=excluded.grant_json,
                revoked=0,
                updated_at=excluded.updated_at
            """,
            (session_id, project, str(grant["mode"]), json.dumps(grant, sort_keys=True), now),
        )
        self._conn.commit()

    def get(self, *, session_id: str, project: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT grant_json, revoked FROM autonomous_mode_grants WHERE session_id=? AND project=?",
            (session_id, project),
        ).fetchone()
        if row is None:
            return None
        grant = json.loads(str(row["grant_json"]))
        grant["revoked"] = bool(row["revoked"])
        return grant

    def revoke(self, *, session_id: str, project: str) -> None:
        self._conn.execute(
            "UPDATE autonomous_mode_grants SET revoked=1, updated_at=? WHERE session_id=? AND project=?",
            (datetime.now(timezone.utc).isoformat(), session_id, project),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


_STORE: AutonomousModeStore | None = None


def get_autonomous_mode_store() -> AutonomousModeStore:
    global _STORE
    if _STORE is None:
        _STORE = AutonomousModeStore()
    return _STORE


def normalize_autonomous_mode(value: Any) -> dict[str, Any]:
    spec = _spec()
    if not isinstance(value, dict):
        return {"mode": str(spec.get("default_mode") or "collaborative_control"), "active": False}
    mode = str(value.get("mode") or spec.get("default_mode") or "collaborative_control").strip()
    task_ids = sorted({str(item).strip() for item in value.get("approved_task_ids") or [] if str(item).strip()})
    framing_versions = {
        str(key).strip(): str(version).strip()
        for key, version in (value.get("task_framing_versions") or {}).items()
        if str(key).strip() and str(version).strip()
    }
    allowed = sorted({str(item).strip() for item in value.get("allowed_actions") or [] if str(item).strip()})
    permissions = value.get("permissions") if isinstance(value.get("permissions"), dict) else {}
    return {
        "mode": mode,
        "active": mode == str(spec.get("autonomous_mode") or "explicit_autonomous_mode"),
        "approval_intent": str(value.get("approval_intent") or "").strip(),
        "approval_ref": str(value.get("approval_ref") or "").strip(),
        "approved_task_ids": task_ids,
        "task_framing_versions": framing_versions,
        "allowed_actions": allowed,
        "expires_at": str(value.get("expires_at") or "").strip(),
        "permissions": {
            "commit": bool(permissions.get("commit", False)),
            "live_mutation": bool(permissions.get("live_mutation", False)),
        },
    }


def evaluate_autonomous_mode(
    grant: dict[str, Any] | None,
    *,
    task_id: str = "",
    action: str = "",
    framing_version: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    spec = _spec()
    normalized = normalize_autonomous_mode(grant)
    stop_reason = ""
    if not normalized["active"]:
        stop_reason = "collaborative_control"
    elif normalized.get("revoked") or bool((grant or {}).get("revoked")):
        stop_reason = "revoked"
    elif normalized["approval_intent"] != str(spec.get("approval_intent") or "explicit_autonomous_mode"):
        stop_reason = "approval_missing"
    elif not normalized["approval_ref"]:
        stop_reason = "approval_missing"
    elif _expired(normalized["expires_at"], now=now):
        stop_reason = "expired"
    elif task_id and task_id not in normalized["approved_task_ids"]:
        stop_reason = "unauthorized_task"
    elif action in set(spec.get("separate_permissions") or []):
        if not normalized["permissions"].get(action, False):
            stop_reason = "separate_permission_required"
    elif action and (
        action not in normalized["allowed_actions"]
        or action not in set(spec.get("allowed_actions") or [])
    ):
        stop_reason = "unauthorized_action"
    elif task_id and not normalized["task_framing_versions"].get(task_id):
        stop_reason = "framing_version_missing"
    elif task_id and framing_version and normalized["task_framing_versions"].get(task_id) != framing_version:
        stop_reason = "framing_version_changed"

    active = not stop_reason
    packet = {
        **normalized,
        "active": active,
        "authority_granted": active,
        "task_relations_grant_authority": False,
        "unspecified_actions_are_denied": True,
        "read_only_actions_remain_available": True,
        "stop_reason": stop_reason,
        "next_safe_action": (
            "Continue only with the approved task and action."
            if active
            else "Continue read-only diagnosis or obtain explicit approval for a new bounded autonomous-mode grant."
        ),
    }
    if stop_reason not in {"", "collaborative_control"}:
        packet["diagnostic_incident"] = build_public_diagnostic_incident(
            kind="autonomous_mode_denied",
            task_id=task_id,
            safe_next_action=packet["next_safe_action"],
        )
    return _compact(packet)


def _expired(value: str, *, now: datetime | None) -> bool:
    if not value:
        return True
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    return expires_at <= current


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}
