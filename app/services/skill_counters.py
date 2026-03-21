"""
Skill Counters Store — SQLite-backed atomic counters for skills.

Replaces Qdrant retrieve+set_payload round-trips for non-vector mutable fields:
  usage_count, helpful_count, usefulness_score, pinned

Why SQLite (not Qdrant):
  - Atomic UPDATE (no read-modify-write race conditions)
  - No embedding needed — pure key/value by skill_id
  - Cheap reads: bulk fetch by skill_id list in one query

Usage:
    store = get_skill_counters()
    await store.increment_helpful("skill-uuid")   # +1 helpful + usage
    await store.increment_usage("skill-uuid")     # +1 usage only
    await store.set_pinned("skill-uuid", True)
    meta = await store.get("skill-uuid")          # -> dict or None
    bulk = await store.get_many(["id1", "id2"])   # -> {id: dict}
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path("qdrant_data") / "skills.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS skill_meta (
    skill_id        TEXT PRIMARY KEY,
    usage_count     INTEGER NOT NULL DEFAULT 0,
    helpful_count   INTEGER NOT NULL DEFAULT 0,
    usefulness_score REAL NOT NULL DEFAULT 1.0,
    pinned          INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_meta_pinned ON skill_meta(pinned);
"""


def _usefulness(helpful: int, usage: int) -> float:
    return round(helpful / max(usage, 1), 3)


class SkillCountersStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()
        logger.info("SkillCountersStore initialized: %s", db_path)

    # ── Writes ────────────────────────────────────────────────────────────────

    async def increment_helpful(self, skill_id: str) -> dict:
        """Atomically increment helpful_count and usage_count. Returns new meta."""
        now = time.time()
        with self._lock:
            self._conn.execute("""
                INSERT INTO skill_meta (skill_id, usage_count, helpful_count, usefulness_score, pinned, updated_at)
                VALUES (?, 1, 1, 1.0, 0, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    usage_count   = usage_count + 1,
                    helpful_count = helpful_count + 1,
                    usefulness_score = ROUND(CAST(helpful_count + 1 AS REAL) / (usage_count + 1), 3),
                    updated_at    = excluded.updated_at
            """, (skill_id, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM skill_meta WHERE skill_id = ?", (skill_id,)
            ).fetchone()
        return dict(row) if row else {}

    async def increment_usage(self, skill_id: str) -> dict:
        """Atomically increment usage_count only (skill used but not marked helpful)."""
        now = time.time()
        with self._lock:
            self._conn.execute("""
                INSERT INTO skill_meta (skill_id, usage_count, helpful_count, usefulness_score, pinned, updated_at)
                VALUES (?, 1, 0, 0.0, 0, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    usage_count   = usage_count + 1,
                    usefulness_score = ROUND(CAST(helpful_count AS REAL) / (usage_count + 1), 3),
                    updated_at    = excluded.updated_at
            """, (skill_id, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM skill_meta WHERE skill_id = ?", (skill_id,)
            ).fetchone()
        return dict(row) if row else {}

    async def set_pinned(self, skill_id: str, pinned: bool) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("""
                INSERT INTO skill_meta (skill_id, usage_count, helpful_count, usefulness_score, pinned, updated_at)
                VALUES (?, 0, 0, 1.0, ?, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    pinned     = excluded.pinned,
                    updated_at = excluded.updated_at
            """, (skill_id, int(pinned), now))
            self._conn.commit()

    async def upsert(self, skill_id: str, usage_count: int = 0, helpful_count: int = 0,
                     pinned: bool = False) -> None:
        """Seed or update counters (used for migration from Qdrant payload)."""
        score = _usefulness(helpful_count, usage_count)
        now = time.time()
        with self._lock:
            self._conn.execute("""
                INSERT INTO skill_meta (skill_id, usage_count, helpful_count, usefulness_score, pinned, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    usage_count      = MAX(excluded.usage_count, usage_count),
                    helpful_count    = MAX(excluded.helpful_count, helpful_count),
                    usefulness_score = excluded.usefulness_score,
                    pinned           = excluded.pinned,
                    updated_at       = excluded.updated_at
            """, (skill_id, usage_count, helpful_count, score, int(pinned), now))
            self._conn.commit()

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get(self, skill_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM skill_meta WHERE skill_id = ?", (skill_id,)
            ).fetchone()
        return dict(row) if row else None

    async def get_many(self, skill_ids: list[str]) -> dict[str, dict]:
        """Bulk fetch. Returns {skill_id: meta_dict}."""
        if not skill_ids:
            return {}
        placeholders = ",".join("?" * len(skill_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM skill_meta WHERE skill_id IN ({placeholders})", skill_ids
            ).fetchall()
        return {row["skill_id"]: dict(row) for row in rows}

    async def get_pinned_ids(self) -> list[str]:
        """Return skill_ids where pinned=1."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT skill_id FROM skill_meta WHERE pinned = 1"
            ).fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[SkillCountersStore] = None


def get_skill_counters() -> SkillCountersStore:
    global _store
    if _store is None:
        _store = SkillCountersStore()
    return _store


def close_skill_counters() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
