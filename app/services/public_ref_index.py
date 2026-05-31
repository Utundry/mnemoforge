from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.system_data_root import data_path

_DB_PATH = data_path("public_ref_index.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS public_ref_index (
    artifact_key       TEXT PRIMARY KEY,
    ref_kind           TEXT NOT NULL,
    project            TEXT NOT NULL,
    local_id           TEXT NOT NULL,
    linked_artifact_key TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT '',
    updated_at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_public_ref_index_project_kind
    ON public_ref_index(project, ref_kind, local_id);

CREATE INDEX IF NOT EXISTS idx_public_ref_index_project
    ON public_ref_index(project, local_id);
"""


class AmbiguousPublicRefError(LookupError):
    def __init__(self, matches: list[dict[str, Any]]) -> None:
        super().__init__("Public ref short id matched multiple artifacts.")
        self.matches = matches


class PublicRefNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class PublicRefResolution:
    artifact_key: str
    ref_kind: str
    project: str
    local_id: str
    source: str
    item: dict[str, Any]


def is_short_public_id(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(r"[0-9a-fA-F]{6,12}", text))


def parse_artifact_key(value: str) -> tuple[str, str, str] | None:
    parts = str(value or "").strip().split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def public_artifact_matches_short_id(item: dict[str, Any], *, short_id: str) -> bool:
    prefix = str(short_id or "").strip().casefold()
    if not prefix:
        return False
    candidates = [
        item.get("local_id"),
        item.get("task_id"),
        item.get("id"),
        item.get("artifact_key"),
        item.get("linked_artifact_key"),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip().casefold()
        if text.startswith(prefix) or f":{prefix}" in text:
            return True
    return False


def canonical_artifact_key_for_short_ref(
    item: dict[str, Any],
    *,
    requested_type: str,
    short_id: str,
) -> str:
    requested = str(requested_type or "").strip().casefold()
    prefix = str(short_id or "").strip().casefold()
    keys = [
        str(item.get("artifact_key") or "").strip(),
        str(item.get("linked_artifact_key") or "").strip(),
    ]
    if requested in {"task", "improvement"}:
        for key in keys:
            lowered = key.casefold()
            if lowered.startswith(f"{requested}:") and f":{prefix}" in lowered:
                return key
    for key in keys:
        if f":{prefix}" in key.casefold():
            return key
    return ""


class PublicRefIndexStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM public_ref_index")
            self._conn.commit()

    def upsert_artifact(self, item: dict[str, Any]) -> None:
        artifact_key = str(item.get("artifact_key") or "").strip()
        parsed = parse_artifact_key(artifact_key)
        if parsed is None:
            return
        ref_kind, project, local_id = parsed
        linked_artifact_key = str(item.get("linked_artifact_key") or "").strip()
        title = str(item.get("title") or "").strip()
        status = str(item.get("status") or "").strip()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO public_ref_index (
                    artifact_key, ref_kind, project, local_id, linked_artifact_key,
                    title, status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_key) DO UPDATE SET
                    ref_kind=excluded.ref_kind,
                    project=excluded.project,
                    local_id=excluded.local_id,
                    linked_artifact_key=excluded.linked_artifact_key,
                    title=excluded.title,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (artifact_key, ref_kind, project, local_id, linked_artifact_key, title, status, time.time()),
            )
            self._conn.commit()

    def upsert_artifacts(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            if isinstance(item, dict):
                self.upsert_artifact(item)

    def remove(self, artifact_key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM public_ref_index WHERE artifact_key=?", (str(artifact_key or "").strip(),))
            self._conn.commit()

    def resolve(self, *, project: str, requested_type: str, short_id: str) -> PublicRefResolution:
        matches = self.find(project=project, requested_type=requested_type, short_id=short_id)
        if not matches:
            raise PublicRefNotFoundError("Public ref short id is not indexed.")
        if len(matches) != 1:
            raise AmbiguousPublicRefError(matches)
        item = matches[0]
        key = canonical_artifact_key_for_short_ref(item, requested_type=requested_type, short_id=short_id)
        if not key:
            raise PublicRefNotFoundError("Public ref short id resolved without a canonical artifact key.")
        parsed = parse_artifact_key(key)
        if parsed is None:
            raise PublicRefNotFoundError("Public ref index contains an invalid artifact key.")
        ref_kind, resolved_project, local_id = parsed
        return PublicRefResolution(
            artifact_key=key,
            ref_kind=ref_kind,
            project=resolved_project,
            local_id=local_id,
            source="public_ref_index",
            item=item,
        )

    def find(self, *, project: str, requested_type: str, short_id: str) -> list[dict[str, Any]]:
        prefix = str(short_id or "").strip().casefold()
        if not prefix:
            return []
        kind = str(requested_type or "").strip().casefold()
        params: list[Any] = [project]
        where = ["project=?"]
        if kind and kind not in {"all", "artifact"}:
            where.append("(ref_kind=? OR linked_artifact_key LIKE ?)")
            params.extend([kind, f"{kind}:{project}:%"])
        params.extend([f"{prefix}%", f"%:{prefix}%", f"%:{prefix}%"])
        query = f"""
            SELECT * FROM public_ref_index
            WHERE {' AND '.join(where)}
              AND (
                lower(local_id) LIKE ?
                OR lower(artifact_key) LIKE ?
                OR lower(linked_artifact_key) LIKE ?
              )
            ORDER BY updated_at DESC
            LIMIT 20
        """
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


_STORE: PublicRefIndexStore | None = None


def get_public_ref_index_store() -> PublicRefIndexStore:
    global _STORE
    if _STORE is None:
        _STORE = PublicRefIndexStore()
    return _STORE


def close_public_ref_index_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE._conn.close()
    _STORE = None
