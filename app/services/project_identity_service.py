from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Optional

from app.config import settings
from app.services.system_data_root import data_path

_DB_PATH = data_path("project_identity.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS project_identity_aliases (
    alias       TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    reason      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_identity_project
    ON project_identity_aliases(project_id, status);
"""


def _clean_project_id(value: object) -> str:
    return str(value or "").strip()[:128]


class ProjectIdentityStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _resolve_unlocked(self, project_id: str | None) -> str:
        clean = _clean_project_id(project_id) or settings.self_project_id
        seen: set[str] = set()
        current = clean
        for _ in range(16):
            if not current or current in seen:
                break
            seen.add(current)
            row = self._conn.execute(
                """
                SELECT project_id
                FROM project_identity_aliases
                WHERE alias = ? AND status = 'active'
                """,
                (current,),
            ).fetchone()
            if not row:
                return current
            next_project = str(row["project_id"])
            if not next_project or next_project == current:
                return current
            current = next_project
        return current or clean

    def upsert_alias(self, *, alias: str, project_id: str, reason: str = "", status: str = "active") -> dict:
        clean_alias = _clean_project_id(alias)
        clean_project = _clean_project_id(project_id)
        if not clean_alias or not clean_project:
            raise ValueError("alias and project_id are required")
        now = time.time()
        with self._lock:
            canonical_project = self._resolve_unlocked(clean_project)
            self._conn.execute(
                """
                INSERT INTO project_identity_aliases (alias, project_id, status, reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    project_id = excluded.project_id,
                    status = excluded.status,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (clean_alias, canonical_project, status or "active", reason or "", now, now),
            )
            self._conn.commit()
        return {
            "alias": clean_alias,
            "project_id": canonical_project,
            "status": status or "active",
            "reason": reason or "",
        }

    def resolve(self, project_id: str | None) -> str:
        with self._lock:
            return self._resolve_unlocked(project_id)

    def aliases_for(self, project_id: str | None, *, include_self: bool = True) -> list[str]:
        canonical = self.resolve(project_id)
        aliases: list[str] = [canonical] if include_self and canonical else []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT alias
                FROM project_identity_aliases
                WHERE status = 'active'
                ORDER BY alias
                """,
            ).fetchall()
        for row in rows:
            alias = str(row["alias"])
            if self._resolve_unlocked(alias) != canonical:
                continue
            if alias and alias not in aliases:
                aliases.append(alias)
        original = _clean_project_id(project_id)
        if original and original not in aliases:
            aliases.append(original)
        return aliases

    def list_aliases(self, project_id: str | None = None) -> list[dict]:
        canonical = self.resolve(project_id) if project_id else ""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT alias, project_id, status, reason, created_at, updated_at
                FROM project_identity_aliases
                WHERE status = 'active'
                ORDER BY project_id, alias
                """,
            ).fetchall()
            items = [dict(row) for row in rows]
            if canonical:
                items = [
                    item for item in items
                    if self._resolve_unlocked(item.get("project_id")) == canonical
                    or self._resolve_unlocked(item.get("alias")) == canonical
                ]
        return items


_STORE: Optional[ProjectIdentityStore] = None


def get_project_identity_store() -> ProjectIdentityStore:
    global _STORE
    if _STORE is None:
        _STORE = ProjectIdentityStore()
        self_project = _clean_project_id(settings.self_project_id)
        if self_project and _STORE.resolve(self_project) == self_project:
            _STORE.upsert_alias(alias=self_project, project_id=self_project, reason="self_project_id")
        public_alias = _clean_project_id(settings.public_project_alias)
        for alias in {public_alias, "sloplesscode", "mnemoforge"}:
            if alias and alias != self_project:
                _STORE.upsert_alias(alias=alias, project_id=self_project or alias, reason="public rename alias")
    return _STORE


def close_project_identity_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None


def resolve_project_id(project_id: str | None) -> str:
    return get_project_identity_store().resolve(project_id)


def project_lookup_ids(project_id: str | None) -> list[str]:
    return get_project_identity_store().aliases_for(project_id)
