"""
Memory Content Store — SQLite-backed content store for Qdrant ref-only architecture.

Stores full content and category-specific metadata keyed by Qdrant point ID,
enabling Qdrant payload to carry only filter fields (category, agent_id,
importance_score, timestamp, tags) while SQLite holds the data.

Pattern (dual-write, Qdrant-fallback):
  Insert: write to BOTH Qdrant (filter payload) AND SQLite (full content+meta)
  Read:   batch fetch from SQLite; fallback to Qdrant payload on cache miss

Categories currently stored here:
  skill          — full SKILL.md content + skill_name, description, platform, etc.
  code_component — code snippet + code_path, symbol, chunk_type, language, imports
  handoff        — full task-packet content + lifecycle metadata for durable pickup
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path("qdrant_data") / "memory_store.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS memory_content (
    memory_id   TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mc_category ON memory_content(category);
CREATE INDEX IF NOT EXISTS idx_mc_updated  ON memory_content(updated_at DESC);
"""


def _decode(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    return d


class MemoryContentStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()
        logger.info("MemoryContentStore initialized: %s", db_path)

    # ── Writes ────────────────────────────────────────────────────────────────

    async def upsert(
        self,
        memory_id: str,
        category: str,
        content: str,
        metadata: dict | None = None,
        created_at: float | None = None,
    ) -> None:
        """Insert or replace a content record."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_content (memory_id, category, content, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    category   = excluded.category,
                    content    = excluded.content,
                    metadata   = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    memory_id,
                    category,
                    content,
                    json.dumps(metadata or {}),
                    created_at if created_at is not None else now,
                    now,
                ),
            )
            self._conn.commit()

    async def patch_metadata(self, memory_id: str, patch: dict) -> None:
        """Merge-update only the metadata JSON field (leave content unchanged)."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata FROM memory_content WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                return
            meta = json.loads(row["metadata"] or "{}")
            meta.update(patch)
            self._conn.execute(
                "UPDATE memory_content SET metadata = ?, updated_at = ? WHERE memory_id = ?",
                (json.dumps(meta), now, memory_id),
            )
            self._conn.commit()

    async def delete(self, memory_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM memory_content WHERE memory_id = ?", (memory_id,)
            )
            self._conn.commit()

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get(self, memory_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_content WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return _decode(row) if row else None

    async def find_by_id_prefix(
        self,
        prefix: str,
        *,
        project: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Find memories by a public id prefix, optionally scoped by project metadata."""
        text = str(prefix or "").strip().casefold()
        if not text:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_content
                WHERE lower(memory_id) LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (f"{text}%", int(limit)),
            ).fetchall()
        decoded = [_decode(row) for row in rows]
        if not project:
            return decoded
        scoped: list[dict] = []
        for row in decoded:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if str(metadata.get("project") or "").strip() == project:
                scoped.append(row)
        return scoped

    async def get_many(self, memory_ids: list[str]) -> dict[str, dict]:
        """Bulk fetch. Returns {memory_id: {content, metadata, ...}}."""
        if not memory_ids:
            return {}
        placeholders = ",".join("?" * len(memory_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM memory_content WHERE memory_id IN ({placeholders})",
                memory_ids,
            ).fetchall()
        return {row["memory_id"]: _decode(row) for row in rows}

    async def exists(self, memory_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM memory_content WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return row is not None

    async def count(self, category: str | None = None) -> int:
        with self._lock:
            if category is None:
                row = self._conn.execute("SELECT COUNT(*) FROM memory_content").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM memory_content WHERE category = ?",
                    (category,),
                ).fetchone()
        return int(row[0]) if row else 0

    async def list_by_category(self, category: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_content
                WHERE category = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (category, limit),
            ).fetchall()
        return [_decode(row) for row in rows]

    async def list_rows(
        self,
        *,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        with self._lock:
            if category is None:
                rows = self._conn.execute(
                    """
                    SELECT * FROM memory_content
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM memory_content
                    WHERE category = ?
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (category, limit, offset),
                ).fetchall()
        return [_decode(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[MemoryContentStore] = None


def get_memory_store() -> MemoryContentStore:
    global _store
    if _store is None:
        _store = MemoryContentStore()
    return _store


def close_memory_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
