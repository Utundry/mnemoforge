from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Iterable, Optional

_DB_PATH = Path("qdrant_data") / "component_docs.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS component_docs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    component_id TEXT NOT NULL,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    implementation TEXT NOT NULL,
    key_files TEXT NOT NULL DEFAULT '[]',
    endpoints TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT '',
    file_hash TEXT NOT NULL DEFAULT '',
    version_note TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '{}',
    extra_payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_component_docs_project_component ON component_docs(project_id, component_id);
CREATE INDEX IF NOT EXISTS idx_component_docs_updated ON component_docs(project_id, updated_at DESC);
"""


def _json_encode(value):
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def _json_decode(value):
    if not value:
        return [] if value == "[]" else {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return {}
    data = dict(row)
    data["key_files"] = _json_decode(data.get("key_files"))
    data["endpoints"] = _json_decode(data.get("endpoints"))
    data["snapshot"] = _json_decode(data.get("snapshot"))
    data["extra_payload"] = _json_decode(data.get("extra_payload"))
    return data


class ComponentDocsStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    def _upsert(
        self,
        *,
        point_id: str,
        project_id: str,
        component_id: str,
        name: str,
        purpose: str,
        implementation: str,
        key_files: Iterable[str],
        endpoints: Iterable[str],
        status: str,
        file_hash: str,
        version_note: str,
        snapshot: dict | None,
        extra_payload: dict | None,
    ) -> None:
        now = time.time()
        key_files_json = json.dumps(list(key_files or []))
        endpoints_json = json.dumps(list(endpoints or []))
        snapshot_json = json.dumps(snapshot or {})
        extra_json = json.dumps(extra_payload or {})
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM component_docs
                WHERE project_id = ? AND component_id = ? AND id != ?
                """,
                (project_id, component_id, point_id),
            )
            existing = self._conn.execute(
                "SELECT created_at FROM component_docs WHERE id = ?",
                (point_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            self._conn.execute(
                """
                INSERT INTO component_docs (
                    id, project_id, component_id, name, purpose, implementation,
                    key_files, endpoints, status, file_hash, version_note,
                    snapshot, extra_payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id = excluded.project_id,
                    component_id = excluded.component_id,
                    name = excluded.name,
                    purpose = excluded.purpose,
                    implementation = excluded.implementation,
                    key_files = excluded.key_files,
                    endpoints = excluded.endpoints,
                    status = excluded.status,
                    file_hash = excluded.file_hash,
                    version_note = excluded.version_note,
                    snapshot = excluded.snapshot,
                    extra_payload = excluded.extra_payload,
                    updated_at = excluded.updated_at
                """,
                (
                    point_id,
                    project_id,
                    component_id,
                    name,
                    purpose,
                    implementation,
                    key_files_json,
                    endpoints_json,
                    status,
                    file_hash,
                    version_note,
                    snapshot_json,
                    extra_json,
                    created_at,
                    now,
                ),
            )
            self._conn.commit()

    async def upsert_component(
        self,
        *,
        point_id: str,
        project_id: str,
        component_id: str,
        name: str,
        purpose: str,
        implementation: str,
        key_files: Iterable[str] | None = None,
        endpoints: Iterable[str] | None = None,
        status: str = "",
        file_hash: str = "",
        version_note: str = "",
        snapshot: dict | None = None,
        extra_payload: dict | None = None,
    ) -> None:
        self._upsert(
            point_id=point_id,
            project_id=project_id,
            component_id=component_id,
            name=name,
            purpose=purpose,
            implementation=implementation,
            key_files=key_files or [],
            endpoints=endpoints or [],
            status=status or "",
            file_hash=file_hash or "",
            version_note=version_note or "",
            snapshot=snapshot or {},
            extra_payload=extra_payload or {},
        )

    async def delete(self, point_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM component_docs WHERE id = ?",
                (point_id,),
            )
            self._conn.commit()

    async def get_by_id(self, point_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM component_docs WHERE id = ?",
                (point_id,),
            ).fetchone()
        return _row_to_dict(row)

    async def get_by_key(self, project_id: str, component_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM component_docs
                WHERE project_id = ? AND component_id = ?
                """,
                (project_id, component_id),
            ).fetchone()
        return _row_to_dict(row)

    async def list_by_project(self, project_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM component_docs
                WHERE project_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_by_project_sync(self, project_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM component_docs
                WHERE project_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def get_many(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM component_docs WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        return {row["id"]: _row_to_dict(row) for row in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORE: Optional[ComponentDocsStore] = None


def get_component_docs_store() -> ComponentDocsStore:
    global _STORE
    if _STORE is None:
        _STORE = ComponentDocsStore()
    return _STORE


def close_component_docs_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None
