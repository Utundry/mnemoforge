from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.system_data_root import data_path

_DB_PATH = data_path("task_reconciliation.db")
REVIEW_DECISIONS = {"supersede", "resolve", "link", "keep_open", "create_follow_up"}
COVERED_DECISIONS = {"supersede", "resolve", "link"}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS task_reconciliation_decisions (
    decision_id TEXT PRIMARY KEY,
    target_task_ref TEXT NOT NULL,
    implemented_task_ref TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    acted_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_reconciliation_target ON task_reconciliation_decisions(target_task_ref, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_reconciliation_impl ON task_reconciliation_decisions(implemented_task_ref, status, updated_at DESC);
"""


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key, fallback in (("evidence_refs", []), ("metadata", {})):
        try:
            data[key] = json.loads(data.get(key) or json.dumps(fallback))
        except json.JSONDecodeError:
            data[key] = fallback
    return data


class TaskReconciliationStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_decision(
        self,
        *,
        target_task_ref: str,
        implemented_task_ref: str = "",
        decision: str,
        reason: str,
        acted_by: str = "",
        evidence_refs: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in REVIEW_DECISIONS:
            raise ValueError(f"unsupported reconciliation decision: {decision}")
        target = str(target_task_ref or "").strip()
        if not target:
            raise ValueError("target_task_ref is required")
        now = time.time()
        decision_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO task_reconciliation_decisions (
                    decision_id, target_task_ref, implemented_task_ref, decision, reason,
                    acted_by, status, created_at, updated_at, evidence_refs, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    target,
                    str(implemented_task_ref or "").strip(),
                    normalized_decision,
                    str(reason or "").strip(),
                    str(acted_by or "").strip(),
                    now,
                    now,
                    json.dumps([str(ref) for ref in (evidence_refs or []) if str(ref).strip()], ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        row = self.get_decision(decision_id)
        assert row is not None
        return row

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_reconciliation_decisions WHERE decision_id = ?",
                (str(decision_id),),
            ).fetchone()
        return _decode(row) if row else None

    def latest_for_target(self, target_task_ref: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM task_reconciliation_decisions
                 WHERE target_task_ref = ? AND status = 'active'
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (str(target_task_ref or "").strip(),),
            ).fetchone()
        return _decode(row) if row else None

    def packet_for_target(self, target_task_ref: str) -> dict[str, Any]:
        target = str(target_task_ref or "").strip()
        decision = self.latest_for_target(target)
        if not decision:
            return {
                "status": "needs_review",
                "target_task_ref": target,
                "reconciliation_warning": "No operator-reviewed reconciliation decision exists for this task.",
                "recommended_actions": sorted(REVIEW_DECISIONS),
                "next_safe_action": "Review whether the task is covered by an implemented task before selecting it as ordinary next work.",
            }
        packet = {
            "status": "reviewed",
            "target_task_ref": target,
            "implemented_task_ref": decision.get("implemented_task_ref"),
            "decision": decision.get("decision"),
            "reason": decision.get("reason"),
            "acted_by": decision.get("acted_by"),
            "decision_id": decision.get("decision_id"),
            "evidence_refs": decision.get("evidence_refs") or [],
            "covered_by_implementation": decision.get("decision") in COVERED_DECISIONS,
            "next_safe_action": _next_safe_action(decision),
        }
        return {key: value for key, value in packet.items() if value not in (None, "", [], {})}


def _next_safe_action(decision: dict[str, Any]) -> str:
    action = str(decision.get("decision") or "")
    if action in COVERED_DECISIONS:
        return "Do not present this task as ordinary next work; show reconciliation evidence or choose another candidate."
    if action == "keep_open":
        return "Task remains open by operator review; it may be selected as normal work."
    if action == "create_follow_up":
        return "Create or select the follow-up task that captures remaining work."
    return "Review reconciliation evidence before choosing next action."


_STORE: TaskReconciliationStore | None = None


def get_task_reconciliation_store() -> TaskReconciliationStore:
    global _STORE
    if _STORE is None:
        _STORE = TaskReconciliationStore()
    return _STORE


def close_task_reconciliation_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None
