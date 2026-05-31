"""
Adaptive State Store — durable SQLite storage for behavioral patterns and
workflow throttle state. Drop-in replacement for the in-memory dicts in
app/routers/skills.py.

Tables:
  behavior_patterns  — per-(agent_id, action_type, context_sig) accept/reject history
  workflow_throttle  — last-emitted timestamp per (agent_id, signal_type)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from time import time
from typing import Optional

from app.services.system_data_root import data_path

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS behavior_patterns (
    agent_id     TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    context_sig  TEXT NOT NULL DEFAULT '',
    accepts      INTEGER NOT NULL DEFAULT 0,
    rejects      INTEGER NOT NULL DEFAULT 0,
    recent_json  TEXT NOT NULL DEFAULT '[]',
    updated_at   REAL NOT NULL,
    PRIMARY KEY (agent_id, action_type, context_sig)
);

CREATE TABLE IF NOT EXISTS workflow_throttle (
    agent_id     TEXT NOT NULL,
    signal_type  TEXT NOT NULL,
    last_emitted REAL NOT NULL,
    PRIMARY KEY (agent_id, signal_type)
);
"""


class AdaptiveStateStore:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    # ── Behavior patterns ────────────────────────────────────────────────────

    def get_pattern(
        self, agent_id: str, action_type: str, context_sig: str = ""
    ) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM behavior_patterns WHERE agent_id=? AND action_type=? AND context_sig=?",
                (agent_id, action_type, context_sig),
            ).fetchone()
        if not row:
            return None
        return {
            "agent_id": row["agent_id"],
            "action_type": row["action_type"],
            "context_sig": row["context_sig"],
            "accepts": row["accepts"],
            "rejects": row["rejects"],
            "recent": json.loads(row["recent_json"]),
        }

    def record_behavior(
        self,
        agent_id: str,
        action_type: str,
        accepted: bool,
        context_sig: str = "",
        recent_window: int = 10,
    ) -> dict:
        """Upsert accept/reject event, return updated pattern dict."""
        with self._lock:
            row = self._conn.execute(
                "SELECT accepts, rejects, recent_json FROM behavior_patterns "
                "WHERE agent_id=? AND action_type=? AND context_sig=?",
                (agent_id, action_type, context_sig),
            ).fetchone()

            if row:
                accepts = row["accepts"] + (1 if accepted else 0)
                rejects = row["rejects"] + (0 if accepted else 1)
                recent: list[bool] = json.loads(row["recent_json"])
            else:
                accepts = 1 if accepted else 0
                rejects = 0 if accepted else 1
                recent = []

            recent.append(accepted)
            if len(recent) > recent_window:
                recent = recent[-recent_window:]

            self._conn.execute(
                "INSERT INTO behavior_patterns (agent_id, action_type, context_sig, accepts, rejects, recent_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id, action_type, context_sig) DO UPDATE SET "
                "accepts=excluded.accepts, rejects=excluded.rejects, "
                "recent_json=excluded.recent_json, updated_at=excluded.updated_at",
                (agent_id, action_type, context_sig, accepts, rejects, json.dumps(recent), time()),
            )
            self._conn.commit()

        return {
            "agent_id": agent_id,
            "action_type": action_type,
            "context_sig": context_sig,
            "accepts": accepts,
            "rejects": rejects,
            "recent": recent,
        }

    def list_patterns(self, agent_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM behavior_patterns WHERE agent_id=? ORDER BY updated_at DESC",
                (agent_id,),
            ).fetchall()
        return [
            {
                "agent_id": r["agent_id"],
                "action_type": r["action_type"],
                "context_sig": r["context_sig"],
                "accepts": r["accepts"],
                "rejects": r["rejects"],
                "recent": json.loads(r["recent_json"]),
            }
            for r in rows
        ]

    def delete_pattern(self, agent_id: str, action_type: str, context_sig: str = "") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM behavior_patterns WHERE agent_id=? AND action_type=? AND context_sig=?",
                (agent_id, action_type, context_sig),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── Workflow throttle ────────────────────────────────────────────────────

    def get_last_emitted(self, agent_id: str, signal_type: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_emitted FROM workflow_throttle WHERE agent_id=? AND signal_type=?",
                (agent_id, signal_type),
            ).fetchone()
        return row["last_emitted"] if row else 0.0

    def set_last_emitted(self, agent_id: str, signal_type: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_throttle (agent_id, signal_type, last_emitted) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id, signal_type) DO UPDATE SET last_emitted=excluded.last_emitted",
                (agent_id, signal_type, ts),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store: Optional[AdaptiveStateStore] = None


def get_adaptive_store(db_path: Path | None = None) -> AdaptiveStateStore:
    global _store
    if _store is None:
        path = db_path or data_path("adaptive_state.db")
        _store = AdaptiveStateStore(path)
    return _store
