from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from app.services.project_identity_service import resolve_project_id
from app.services.system_data_root import data_path

_DB_PATH = data_path("context_pages.db")
ACTIVE_STATUS = "active"
INACTIVE_STATUSES = {"superseded", "archived"}
VALID_STATUSES = {"active", "superseded", "archived", "draft"}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS context_pages (
    page_id TEXT PRIMARY KEY,
    logical_page_id TEXT NOT NULL,
    parent_ref TEXT NOT NULL,
    project TEXT NOT NULL,
    page_kind TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    superseded_by_page_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_context_pages_parent ON context_pages(parent_ref, status, page_index);
CREATE INDEX IF NOT EXISTS idx_context_pages_project ON context_pages(project, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_pages_logical ON context_pages(logical_page_id, version DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_pages_active_index
    ON context_pages(parent_ref, page_index)
    WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_pages_active_entry
    ON context_pages(parent_ref)
    WHERE status = 'active' AND page_kind = 'entry';
"""


def page_ref(page_id: str) -> str:
    return f"context_page:{page_id}"


def _now() -> float:
    return time.time()


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.get("metadata") or "{}")
    except json.JSONDecodeError:
        data["metadata"] = {}
    data["page_ref"] = page_ref(str(data.get("page_id") or ""))
    data["has_more"] = False
    return data


class ContextPageIntegrityError(ValueError):
    pass


class ContextPageStore:
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

    def create_page(
        self,
        *,
        parent_ref: str,
        project: str,
        page_kind: str,
        page_index: int,
        title: str = "",
        summary: str = "",
        content: str = "",
        status: str = ACTIVE_STATUS,
        created_by: str = "",
        metadata: dict[str, Any] | None = None,
        logical_page_id: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        parent_ref = _clean_required(parent_ref, "parent_ref")
        project = resolve_project_id(_clean_required(project, "project"))
        page_kind = _clean_required(page_kind, "page_kind")
        status = _normalize_status(status)
        page_index = int(page_index)
        if page_index < 1:
            raise ContextPageIntegrityError("page_index must be >= 1")
        if page_kind == "entry" and page_index != 1:
            raise ContextPageIntegrityError("entry page must use page_index=1")
        page_id = str(page_id or uuid.uuid4())
        logical_page_id = str(logical_page_id or uuid.uuid4())
        now = _now()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO context_pages (
                        page_id, logical_page_id, parent_ref, project, page_kind,
                        page_index, title, summary, content, version, status,
                        superseded_by_page_id, created_at, updated_at,
                        created_by, updated_by, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        page_id,
                        logical_page_id,
                        parent_ref,
                        project,
                        page_kind,
                        page_index,
                        str(title or ""),
                        str(summary or ""),
                        str(content or ""),
                        1,
                        status,
                        now,
                        now,
                        str(created_by or ""),
                        str(created_by or ""),
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ContextPageIntegrityError(str(exc)) from exc
        row = self.get_page(page_id=page_id, include_history=True)
        assert row is not None
        return row

    def supersede_page(
        self,
        *,
        page_id: str,
        title: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        updated_by: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_page(page_id=page_id)
        if not current:
            raise ContextPageIntegrityError(f"active page not found: {page_id}")
        new_page_id = str(uuid.uuid4())
        now = _now()
        merged_metadata = dict(current.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE context_pages
                       SET status = 'superseded', superseded_by_page_id = ?, updated_at = ?, updated_by = ?
                     WHERE page_id = ? AND status = 'active'
                    """,
                    (new_page_id, now, str(updated_by or ""), page_id),
                )
                self._conn.execute(
                    """
                    INSERT INTO context_pages (
                        page_id, logical_page_id, parent_ref, project, page_kind,
                        page_index, title, summary, content, version, status,
                        superseded_by_page_id, created_at, updated_at,
                        created_by, updated_by, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_page_id,
                        current["logical_page_id"],
                        current["parent_ref"],
                        current["project"],
                        current["page_kind"],
                        int(current["page_index"]),
                        current["title"] if title is None else str(title or ""),
                        current["summary"] if summary is None else str(summary or ""),
                        current["content"] if content is None else str(content or ""),
                        int(current["version"] or 1) + 1,
                        now,
                        now,
                        str(current.get("created_by") or ""),
                        str(updated_by or ""),
                        json.dumps(merged_metadata, ensure_ascii=False),
                    ),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ContextPageIntegrityError(str(exc)) from exc
        row = self.get_page(page_id=new_page_id, include_history=True)
        assert row is not None
        return row

    def archive_page(self, *, page_id: str, updated_by: str = "") -> dict[str, Any]:
        row = self.get_page(page_id=page_id, include_history=True)
        if not row:
            raise ContextPageIntegrityError(f"page not found: {page_id}")
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE context_pages SET status = 'archived', updated_at = ?, updated_by = ? WHERE page_id = ?",
                (now, str(updated_by or ""), page_id),
            )
            self._conn.commit()
        archived = self.get_page(page_id=page_id, include_history=True)
        assert archived is not None
        return archived

    def get_page(self, *, page_id: str, include_history: bool = False) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM context_pages WHERE page_id = ?", (page_id,)).fetchone()
        if not row:
            return None
        data = _decode(row)
        if not include_history and data.get("status") != ACTIVE_STATUS:
            return None
        return data

    def get_entry_page(self, *, parent_ref: str, include_history: bool = False) -> dict[str, Any] | None:
        status_clause = "" if include_history else "AND status = 'active'"
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT * FROM context_pages
                 WHERE parent_ref = ? AND page_kind = 'entry' {status_clause}
                 ORDER BY status = 'active' DESC, version DESC, updated_at DESC
                 LIMIT 1
                """,
                (parent_ref,),
            ).fetchone()
        return _decode(row) if row else None

    def list_pages(self, *, parent_ref: str, include_history: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        status_clause = "" if include_history else "AND status = 'active'"
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM context_pages
                 WHERE parent_ref = ? {status_clause}
                 ORDER BY page_index ASC, version DESC
                 LIMIT ?
                """,
                (parent_ref, int(limit)),
            ).fetchall()
        return [_decode(row) for row in rows]

    def entry_packet(self, *, parent_ref: str, include_history: bool = False, limit: int = 50) -> dict[str, Any] | None:
        entry = self.get_entry_page(parent_ref=parent_ref, include_history=include_history)
        if not entry:
            return None
        pages = self.list_pages(parent_ref=parent_ref, include_history=include_history, limit=limit)
        toc = [compact_page(page, include_content=False) for page in pages]
        additional = [page for page in pages if page["page_id"] != entry["page_id"]]
        packet = compact_page(entry, include_content=True)
        packet["pages"] = toc
        packet["has_more"] = bool(additional)
        if additional:
            packet["next_page_ref"] = additional[0]["page_ref"]
        return packet

    def ordinary_indexable_pages(self, *, project: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        project_clause = ""
        if project:
            project_clause = "AND project = ?"
            params.append(resolve_project_id(project))
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM context_pages
                 WHERE status = 'active' {project_clause}
                 ORDER BY updated_at DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [_decode(row) for row in rows]


def compact_page(page: dict[str, Any], *, include_content: bool = False) -> dict[str, Any]:
    keys = [
        "page_ref", "page_id", "logical_page_id", "parent_ref", "project",
        "page_kind", "page_index", "title", "summary", "version", "status",
        "superseded_by_page_id", "updated_at",
    ]
    result = {key: page.get(key) for key in keys if page.get(key) not in (None, "", [])}
    if include_content and page.get("content") not in (None, ""):
        result["content"] = page.get("content")
    return result


def index_payload_for_page(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_ref": page.get("page_ref"),
        "parent_ref": page.get("parent_ref"),
        "project": page.get("project"),
        "page_kind": page.get("page_kind"),
        "version": page.get("version"),
        "status": page.get("status"),
        "category": "context_page",
    }


def _clean_required(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContextPageIntegrityError(f"{field} is required")
    return text


def _normalize_status(status: str) -> str:
    normalized = str(status or ACTIVE_STATUS).strip().lower() or ACTIVE_STATUS
    if normalized not in VALID_STATUSES:
        raise ContextPageIntegrityError(f"unsupported page status: {normalized}")
    return normalized


_STORE: ContextPageStore | None = None


def get_context_page_store() -> ContextPageStore:
    global _STORE
    if _STORE is None:
        _STORE = ContextPageStore()
    return _STORE


def close_context_page_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None
