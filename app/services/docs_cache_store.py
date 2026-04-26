from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

_DB_PATH = Path("qdrant_data") / "docs_cache.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS docs_cache (
    project     TEXT PRIMARY KEY,
    status_json TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_cache_updated ON docs_cache(updated_at DESC);
"""


class DocsCacheStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()
        logger.info("DocsCacheStore initialized: %s", db_path)

    def get(self, project: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT project, status_json, updated_at FROM docs_cache WHERE project = ?",
                (project,),
            ).fetchone()
        if row is None:
            return None
        return {
            "project": row["project"],
            "status_json": row["status_json"],
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def upsert(self, project: str, status_json: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO docs_cache (project, status_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(project) DO UPDATE SET
                    status_json = excluded.status_json,
                    updated_at = excluded.updated_at
                """,
                (project, status_json, now),
            )
            self._conn.commit()

    def delete(self, project: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs_cache WHERE project = ?", (project,))
            self._conn.commit()

    def list_projects(self, limit: int = 1000) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT project FROM docs_cache
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row["project"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store: DocsCacheStore | None = None


def get_docs_cache_store() -> DocsCacheStore:
    global _store
    if _store is None:
        _store = DocsCacheStore()
    return _store


def close_docs_cache_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
