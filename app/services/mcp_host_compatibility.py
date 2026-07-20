from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec, workflow_spec_cache
from app.services.system_data_root import data_path
from app.services.task_lease_service import get_task_lease_store, verify_work_token_for_mutation


_DB_PATH = data_path("mcp_host_compatibility.db")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_host_compatibility (
    identity_key TEXT PRIMARY KEY,
    first_session_hash TEXT NOT NULL DEFAULT '',
    latest_session_hash TEXT NOT NULL DEFAULT '',
    session_count INTEGER NOT NULL DEFAULT 0,
    traits_json TEXT NOT NULL DEFAULT '[]',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_compatibility_cooldowns (
    scope_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY(scope_key, event_key)
);
CREATE INDEX IF NOT EXISTS idx_mcp_compatibility_expiry
    ON mcp_host_compatibility(expires_at);
CREATE INDEX IF NOT EXISTS idx_mcp_compatibility_cooldown_expiry
    ON mcp_compatibility_cooldowns(expires_at);
"""


@workflow_spec_cache(maxsize=1)
def _spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/host_compatibility.json")
    except Exception:
        return {
            "session_churn_window_seconds": 900,
            "cooldown_seconds": 300,
            "state_ttl_seconds": 14400,
        }


class McpHostCompatibilityStore:
    def __init__(self, path: Path = _DB_PATH) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = Lock()
        self._conn.executescript(_SCHEMA)

    def observe(
        self,
        *,
        agent_fingerprint: str,
        session_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = float(now if now is not None else time.time())
        identity_key = _digest(agent_fingerprint)
        session_hash = _digest(session_id)
        if not identity_key:
            return _default_profile()
        spec = _spec()
        churn_window = int(spec.get("session_churn_window_seconds") or 900)
        ttl = int(spec.get("state_ttl_seconds") or 14400)
        with self._lock:
            self._purge_expired(current)
            row = self._conn.execute(
                "SELECT * FROM mcp_host_compatibility WHERE identity_key=?",
                (identity_key,),
            ).fetchone()
            traits = set(json.loads(str(row["traits_json"]))) if row else set()
            session_count = int(row["session_count"]) if row else 0
            first_session = str(row["first_session_hash"]) if row else session_hash
            if session_hash:
                if row and session_hash != str(row["latest_session_hash"]) and current - float(row["last_seen"]) <= churn_window:
                    traits.add("session_churn")
                elif "session_churn" not in traits:
                    traits.add("stable_session")
                if not row or session_hash != str(row["latest_session_hash"]):
                    session_count += 1
            self._conn.execute(
                """
                INSERT INTO mcp_host_compatibility(
                    identity_key, first_session_hash, latest_session_hash,
                    session_count, traits_json, first_seen, last_seen, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    latest_session_hash=excluded.latest_session_hash,
                    session_count=excluded.session_count,
                    traits_json=excluded.traits_json,
                    last_seen=excluded.last_seen,
                    expires_at=excluded.expires_at
                """,
                (
                    identity_key,
                    first_session,
                    session_hash,
                    session_count,
                    json.dumps(sorted(traits)),
                    float(row["first_seen"]) if row else current,
                    current,
                    current + ttl,
                ),
            )
            self._conn.commit()
        return {
            "identity_key": f"agent:{identity_key}",
            "session_behavior": "stateless_or_one_shot" if "session_churn" in traits else "stable_or_unknown",
            "traits": sorted(traits),
            "observed_session_count": session_count,
            "host_labels_are_advisory": True,
        }

    def check_cooldown(
        self,
        *,
        scope_key: str,
        event_key: str,
        cooldown_seconds: int | None = None,
        now: float | None = None,
    ) -> bool:
        current = float(now if now is not None else time.time())
        cooldown = int(cooldown_seconds or _spec().get("cooldown_seconds") or 300)
        if not scope_key or not event_key:
            return False
        with self._lock:
            self._purge_expired(current)
            row = self._conn.execute(
                "SELECT last_seen FROM mcp_compatibility_cooldowns WHERE scope_key=? AND event_key=?",
                (scope_key, event_key),
            ).fetchone()
            repeated = bool(row and current - float(row["last_seen"]) < cooldown)
            self._conn.execute(
                """
                INSERT INTO mcp_compatibility_cooldowns(scope_key, event_key, last_seen, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_key, event_key) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    expires_at=excluded.expires_at
                """,
                (scope_key, event_key, current, current + cooldown),
            )
            self._conn.commit()
        return repeated

    def _purge_expired(self, now: float) -> None:
        self._conn.execute("DELETE FROM mcp_host_compatibility WHERE expires_at <= ?", (now,))
        self._conn.execute("DELETE FROM mcp_compatibility_cooldowns WHERE expires_at <= ?", (now,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORE: McpHostCompatibilityStore | None = None


def get_mcp_host_compatibility_store() -> McpHostCompatibilityStore:
    global _STORE
    if _STORE is None:
        _STORE = McpHostCompatibilityStore()
    return _STORE


def resolve_task_continuity_scope(
    *,
    project: str,
    task_id: str,
    work_token: str,
) -> dict[str, Any]:
    if not project or not task_id or not work_token:
        return {}
    lease_store = get_task_lease_store()
    active = lease_store.get_active_claim(project=project, task_id=task_id)
    if active is None:
        return {}
    if not verify_work_token_for_mutation(
        store=lease_store,
        lease_id=active.lease_id,
        work_token=work_token,
        task_id=task_id,
        project=project,
    ):
        return {}
    return {
        "session_scope": active.session_id,
        "scope_key": f"task:{_digest(project + ':' + task_id + ':' + work_token)}",
        "trait": "task_bound_continuity",
        "lease_id": active.lease_id,
        "expires_at": active.expires_at.isoformat(),
    }


def _digest(value: str) -> str:
    cleaned = str(value or "").strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:20] if cleaned else ""


def _default_profile() -> dict[str, Any]:
    return {
        "session_behavior": "unknown",
        "traits": [],
        "observed_session_count": 0,
        "host_labels_are_advisory": True,
    }
