from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.system_data_root import data_path

_DB_PATH = data_path("mcp_feature_gates.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mcp_feature_gates (
    feature_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT 'system',
    updated_at REAL NOT NULL,
    PRIMARY KEY (feature_id, scope, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_feature_gates_scope ON mcp_feature_gates(scope, scope_id);
"""


class McpFeatureGateStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    def set_gate(
        self,
        *,
        feature_id: str,
        scope: str,
        scope_id: str,
        enabled: bool,
        reason: str = "",
        updated_by: str = "system",
    ) -> dict[str, Any]:
        now = time.time()
        normalized_scope = _normalize_scope(scope)
        normalized_scope_id = str(scope_id or "default").strip() or "default"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO mcp_feature_gates
                    (feature_id, scope, scope_id, enabled, reason, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feature_id, scope, scope_id)
                DO UPDATE SET enabled = excluded.enabled,
                              reason = excluded.reason,
                              updated_by = excluded.updated_by,
                              updated_at = excluded.updated_at
                """,
                (
                    str(feature_id).strip(),
                    normalized_scope,
                    normalized_scope_id,
                    1 if enabled else 0,
                    str(reason or ""),
                    str(updated_by or "system"),
                    now,
                ),
            )
            self._conn.commit()
        return {
            "feature_id": str(feature_id).strip(),
            "scope": normalized_scope,
            "scope_id": normalized_scope_id,
            "enabled": bool(enabled),
            "reason": str(reason or ""),
            "updated_by": str(updated_by or "system"),
            "updated_at": now,
        }

    def get_gate(self, *, feature_id: str, scope: str, scope_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM mcp_feature_gates
                WHERE feature_id = ? AND scope = ? AND scope_id = ?
                """,
                (str(feature_id).strip(), _normalize_scope(scope), str(scope_id or "default").strip() or "default"),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_gates(self, *, scope: str | None = None, scope_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope:
            clauses.append("scope = ?")
            params.append(_normalize_scope(scope))
        if scope_id:
            clauses.append("scope_id = ?")
            params.append(str(scope_id).strip())
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM mcp_feature_gates {where} ORDER BY updated_at DESC",
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def is_enabled(self, *, feature_id: str, default_enabled: bool = True, scope_chain: list[tuple[str, str]] | None = None) -> bool:
        for scope, scope_id in scope_chain or []:
            gate = self.get_gate(feature_id=feature_id, scope=scope, scope_id=scope_id)
            if gate is not None:
                return bool(gate["enabled"])
        return bool(default_enabled)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _normalize_scope(scope: str) -> str:
    value = str(scope or "session").strip().lower()
    return value if value in {"session", "runtime_profile", "agent", "project", "global"} else "session"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "feature_id": row["feature_id"],
        "scope": row["scope"],
        "scope_id": row["scope_id"],
        "enabled": bool(row["enabled"]),
        "reason": row["reason"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


_STORE: McpFeatureGateStore | None = None


def get_mcp_feature_gate_store() -> McpFeatureGateStore:
    global _STORE
    if _STORE is None:
        _STORE = McpFeatureGateStore()
    return _STORE


def close_mcp_feature_gate_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None
