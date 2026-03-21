"""
Improvements Store — SQLite-backed storage for improvement records.

Schema:
    improvements(id PK, project, title, norm_title, description, status,
                 importance_score, tags JSON, agent_id, created_at REAL,
                 resolved_at REAL|NULL, report_count INT, report_history JSON)

Dedup: upsert_by_title() matches on (norm_title, project, status='open').
On collision: merges tags, bumps importance_score, appends to report_history.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

_DB_PATH = Path("qdrant_data") / "improvements.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS improvements (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL DEFAULT 'supermemory',
    title           TEXT NOT NULL,
    norm_title      TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    importance_score REAL NOT NULL DEFAULT 0.7,
    tags            TEXT NOT NULL DEFAULT '[]',
    agent_id        TEXT NOT NULL DEFAULT 'llm',
    created_at      REAL NOT NULL,
    resolved_at     REAL,
    report_count    INTEGER NOT NULL DEFAULT 1,
    report_history  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_improvements_project   ON improvements(project);
CREATE INDEX IF NOT EXISTS idx_improvements_status    ON improvements(status);
CREATE INDEX IF NOT EXISTS idx_improvements_created   ON improvements(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_improvements_norm      ON improvements(norm_title, project, status);
"""

_MIGRATE_SQL = [
    "ALTER TABLE improvements ADD COLUMN norm_title TEXT DEFAULT ''",
    "ALTER TABLE improvements ADD COLUMN report_count INTEGER DEFAULT 1",
    "ALTER TABLE improvements ADD COLUMN report_history TEXT DEFAULT '[]'",
    "ALTER TABLE improvements ADD COLUMN node_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_improvements_norm ON improvements(norm_title, project, status)",
]


def _normalize_title(title: str) -> str:
    """Canonical form for dedup: lowercase, punctuation→space, collapse whitespace."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["report_history"] = json.loads(d.get("report_history") or "[]")
    d["report_count"] = d.get("report_count") or 1
    d["node_id"] = d.get("node_id") or ""
    return d


class ImprovementsStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()
            self._migrate()
            self._backfill_norm_titles()
        logger.info("ImprovementsStore initialized: %s", db_path)

    def _migrate(self) -> None:
        """Add new columns to existing DB without dropping data."""
        for sql in _MIGRATE_SQL:
            try:
                self._conn.execute(sql)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    def _backfill_norm_titles(self) -> None:
        """Fill norm_title for existing rows that have empty norm_title."""
        rows = self._conn.execute(
            "SELECT id, title FROM improvements WHERE norm_title = ''"
        ).fetchall()
        for row in rows:
            norm = _normalize_title(row["title"])
            self._conn.execute(
                "UPDATE improvements SET norm_title = ? WHERE id = ?",
                (norm, row["id"]),
            )
        if rows:
            self._conn.commit()
            logger.info("Backfilled norm_title for %d improvements", len(rows))

    # ── Writes ────────────────────────────────────────────────────────────────

    async def insert(
        self,
        *,
        title: str,
        description: str,
        project: str = "supermemory",
        agent_id: str = "llm",
        importance_score: float = 0.7,
        tags: list[str] | None = None,
        improvement_id: Optional[UUID] = None,
        created_at: Optional[float] = None,
    ) -> UUID:
        """Raw insert — bypasses dedup. Prefer upsert_by_title for normal use."""
        uid = improvement_id or uuid4()
        now = created_at if created_at is not None else time.time()
        norm = _normalize_title(title)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO improvements
                    (id, project, title, norm_title, description, status,
                     importance_score, tags, agent_id, created_at, report_count, report_history)
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 1, '[]')
                """,
                (str(uid), project, title, norm, description,
                 importance_score, json.dumps(tags or []), agent_id, now),
            )
            self._conn.commit()
        return uid

    async def upsert_by_title(
        self,
        *,
        title: str,
        description: str,
        project: str = "supermemory",
        agent_id: str = "llm",
        importance_score: float = 0.7,
        tags: list[str] | None = None,
    ) -> tuple[UUID, bool]:
        """
        Insert improvement only if no open record with same (norm_title, project) exists.
        On collision: merge tags, bump importance_score to max, append report to history.
        Returns (id, created: bool).
        """
        norm = _normalize_title(title)
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, tags, importance_score, report_count, report_history "
                "FROM improvements WHERE norm_title = ? AND project = ? AND status = 'open'",
                (norm, project),
            ).fetchone()

            if existing:
                # Merge tags
                old_tags: list[str] = json.loads(existing["tags"] or "[]")
                merged_tags = list(dict.fromkeys(old_tags + (tags or [])))
                # Bump importance to max
                new_score = max(existing["importance_score"], importance_score)
                # Append report to history
                history: list[dict] = json.loads(existing["report_history"] or "[]")
                history.append({
                    "ts": now,
                    "agent_id": agent_id,
                    "importance_score": importance_score,
                    "description_snippet": description[:200],
                })
                self._conn.execute(
                    """UPDATE improvements
                       SET tags = ?, importance_score = ?, report_count = report_count + 1,
                           report_history = ?
                       WHERE id = ?""",
                    (json.dumps(merged_tags), new_score, json.dumps(history), existing["id"]),
                )
                self._conn.commit()
                return UUID(existing["id"]), False

            uid = uuid4()
            self._conn.execute(
                """
                INSERT INTO improvements
                    (id, project, title, norm_title, description, status,
                     importance_score, tags, agent_id, created_at, report_count, report_history)
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 1, '[]')
                """,
                (str(uid), project, title, norm, description,
                 importance_score, json.dumps(tags or []), agent_id, now),
            )
            self._conn.commit()
        return uid, True

    def set_node_id(self, improvement_id: UUID, node_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE improvements SET node_id = ? WHERE id = ?",
                (node_id, str(improvement_id)),
            )
            self._conn.commit()

    async def resolve(self, improvement_id: UUID) -> Optional[str]:
        """Mark as resolved. Returns project name or None if not found."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT project FROM improvements WHERE id = ?", (str(improvement_id),)
            ).fetchone()
            if row is None:
                return None
            project = row["project"]
            self._conn.execute(
                "UPDATE improvements SET status = 'resolved', resolved_at = ? WHERE id = ?",
                (now, str(improvement_id)),
            )
            self._conn.commit()
        return project

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get(self, improvement_id: UUID) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM improvements WHERE id = ?", (str(improvement_id),)
            ).fetchone()
        return _row_to_dict(row) if row else None

    async def list(
        self,
        project: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM improvements {where} ORDER BY importance_score DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    async def count_by_status(self, project: Optional[str] = None) -> dict[str, int]:
        clauses: list[str] = []
        params: list = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT status, COUNT(*) as n FROM improvements {where} GROUP BY status",
                params,
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[ImprovementsStore] = None


def get_improvements_store() -> ImprovementsStore:
    global _store
    if _store is None:
        _store = ImprovementsStore()
    return _store


def close_improvements_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
