"""
Project Knowledge Tree — SQLite store.

Tables:
  tree_nodes        — hierarchical nodes (idea/project/area/task/leaf)
  project_workspaces — directory-to-project mapping with isolation
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Optional
from uuid import UUID, uuid4

from app.services.system_data_root import data_path

logger = logging.getLogger(__name__)

_DB_PATH = data_path("project_tree.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tree_nodes (
    id               TEXT PRIMARY KEY,
    parent_id        TEXT,
    type             TEXT NOT NULL DEFAULT 'idea',
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    goal             TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'inbox',
    topic_path       TEXT NOT NULL DEFAULT '',
    doc              TEXT NOT NULL DEFAULT '',
    doc_candidate    TEXT NOT NULL DEFAULT '',
    doc_generated_at REAL,
    doc_candidate_generated_at REAL,
    sort_order       INTEGER NOT NULL DEFAULT 0,
    tags             TEXT NOT NULL DEFAULT '[]',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    done_at          REAL,
    meta_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tree_parent  ON tree_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_tree_status  ON tree_nodes(status);
CREATE INDEX IF NOT EXISTS idx_tree_topic   ON tree_nodes(topic_path);
CREATE INDEX IF NOT EXISTS idx_tree_type    ON tree_nodes(type);

CREATE TABLE IF NOT EXISTS node_journal_entries (
    id          TEXT PRIMARY KEY,
    node_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_node ON node_journal_entries(node_id, created_at);

CREATE TABLE IF NOT EXISTS project_workspaces (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    dir_path     TEXT NOT NULL UNIQUE,
    canonical    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   REAL NOT NULL,
    promoted_at  REAL,
    meta_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ws_project ON project_workspaces(project_id);
CREATE INDEX IF NOT EXISTS idx_ws_dir     ON project_workspaces(dir_path);
"""


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["meta_json"] = json.loads(d.get("meta_json") or "{}")
    return d


def _ws_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["canonical"] = bool(d.get("canonical"))
    d["meta_json"] = json.loads(d.get("meta_json") or "{}")
    return d


def _merge_tags(*tag_lists: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for tags in tag_lists:
        for tag in tags or []:
            key = str(tag).strip()
            if not key or key in seen:
                continue
            merged.append(key)
            seen.add(key)
    return merged


def _merge_meta_json(preferred: dict, duplicate: dict) -> dict:
    merged = dict(preferred or {})
    for key, value in (duplicate or {}).items():
        existing = merged.get(key)
        if key not in merged or existing is None or existing == "" or existing == [] or existing == {}:
            merged[key] = value
            continue
        if isinstance(existing, list) and isinstance(value, list):
            merged[key] = _merge_tags(existing, value)
    return merged


class ProjectTreeStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._migrate()
            self._conn.commit()
        logger.info("ProjectTreeStore initialized: %s", db_path)

    def _migrate(self) -> None:
        """Add columns and tables introduced after initial schema creation."""
        for col, definition in [
            ("doc_candidate", "TEXT NOT NULL DEFAULT ''"),
            ("doc_candidate_generated_at", "REAL"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE tree_nodes ADD COLUMN {col} {definition}")
                logger.info("Migration: added column %s to tree_nodes", col)
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE project_workspaces ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
            logger.info("Migration: added column meta_json to project_workspaces")
        except sqlite3.OperationalError:
            pass
        # Ensure journal table exists (idempotent — CREATE TABLE IF NOT EXISTS)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS node_journal_entries (
                id         TEXT PRIMARY KEY,
                node_id    TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_journal_node
                ON node_journal_entries(node_id, created_at);
        """)

    # ── Node writes ───────────────────────────────────────────────────────────

    def create_node(
        self,
        *,
        title: str,
        type: str = "idea",
        parent_id: Optional[str] = None,
        description: str = "",
        goal: str = "",
        status: str = "inbox",
        topic_path: str = "",
        tags: list[str] | None = None,
        sort_order: int = 0,
    ) -> str:
        uid = str(uuid4())
        now = time.time()
        # Auto-derive topic_path if not set
        if not topic_path and parent_id:
            parent = self.get_node(parent_id)
            if parent and parent.get("topic_path"):
                slug = re.sub(r"[^\w]+", "-", title.lower()).strip("-")
                topic_path = f"{parent['topic_path']}/{slug}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO tree_nodes
                   (id,parent_id,type,title,description,goal,status,
                    topic_path,tags,sort_order,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, parent_id, type, title, description, goal, status,
                 topic_path, json.dumps(tags or []), sort_order, now, now),
            )
            self._conn.commit()
        return uid

    def update_node(self, node_id: str, **fields) -> bool:
        allowed = {"title", "description", "goal", "status", "topic_path",
                   "doc", "doc_candidate", "doc_generated_at", "doc_candidate_generated_at", "tags", "sort_order",
                   "parent_id", "done_at", "meta_json"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "meta_json" in updates and isinstance(updates["meta_json"], dict):
            updates["meta_json"] = json.dumps(updates["meta_json"])
        if "status" in updates and updates["status"] == "done":
            updates.setdefault("done_at", time.time())
        updates["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [node_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tree_nodes SET {cols} WHERE id = ?", vals
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_node(self, node_id: str) -> bool:
        return self.update_node(node_id, status="archived")

    # ── Node reads ────────────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tree_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        return _row(row) if row else None

    def get_by_topic_path(self, topic_path: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tree_nodes WHERE topic_path = ? LIMIT 1",
                (topic_path,),
            ).fetchone()
        return _row(row) if row else None

    def find_node_by_improvement_id(self, improvement_id: str) -> Optional[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tree_nodes
                WHERE type = 'task'
                ORDER BY created_at ASC
                """
            ).fetchall()
        for row in rows:
            parsed = _row(row)
            meta = parsed.get("meta_json") or {}
            if str(meta.get("improvement_id") or "") == str(improvement_id):
                return parsed
        return None

    def find_equivalent_node(
        self,
        *,
        title: str,
        type: str,
        parent_id: Optional[str],
        status: Optional[str] = None,
        topic_path: Optional[str] = None,
    ) -> Optional[dict]:
        clauses = ["type = ?", "title = ?"]
        params: list[object] = [type, title]
        if parent_id is None:
            clauses.append("parent_id IS NULL")
        else:
            clauses.append("parent_id = ?")
            params.append(parent_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if topic_path is not None:
            clauses.append("topic_path = ?")
            params.append(topic_path)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM tree_nodes WHERE {' AND '.join(clauses)} ORDER BY created_at ASC LIMIT 1",
                params,
            ).fetchone()
        return _row(row) if row else None

    def get_children(self, parent_id: Optional[str], include_archived: bool = False) -> list[dict]:
        clause = "parent_id IS NULL" if parent_id is None else "parent_id = ?"
        params: list = [] if parent_id is None else [parent_id]
        if not include_archived:
            clause += " AND status != 'archived'"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tree_nodes WHERE {clause} ORDER BY sort_order, created_at",
                params,
            ).fetchall()
        return [_row(r) for r in rows]

    def list_nodes(
        self,
        status: Optional[str] = None,
        type: Optional[str] = None,
        topic_prefix: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?"); params.append(status)
        if type:
            clauses.append("type = ?"); params.append(type)
        if topic_prefix:
            clauses.append("topic_path LIKE ?"); params.append(f"{topic_prefix}%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tree_nodes {where} ORDER BY sort_order, created_at LIMIT ?",
                params,
            ).fetchall()
        return [_row(r) for r in rows]

    def get_inbox(self) -> list[dict]:
        """Ideas without a parent (loose, unassigned)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tree_nodes WHERE parent_id IS NULL AND type='idea' "
                "AND status != 'archived' ORDER BY created_at DESC"
            ).fetchall()
        return [_row(r) for r in rows]

    def get_projects(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tree_nodes WHERE type='project' AND status != 'archived' "
                "ORDER BY sort_order, title"
            ).fetchall()
        return [_row(r) for r in rows]

    def build_subtree(self, node_id: Optional[str] = None, depth: int = 0, max_depth: int = 8) -> list[dict]:
        """Recursively build tree starting from node_id (None = all roots)."""
        nodes = self.get_children(node_id)
        result = []
        for n in nodes:
            n["_depth"] = depth
            n["children"] = self.build_subtree(n["id"], depth + 1, max_depth) if depth < max_depth else []
            result.append(n)
        return result

    # ── Workspace writes ──────────────────────────────────────────────────────

    def register_workspace(self, *, project_id: str, dir_path: str, canonical: bool = False) -> str:
        uid = str(uuid4())
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM project_workspaces WHERE dir_path = ?", (dir_path,)
            ).fetchone()
            if existing:
                return existing["id"]
            if canonical:
                self._conn.execute(
                    "UPDATE project_workspaces SET canonical=0 WHERE project_id=?",
                    (project_id,),
                )
            self._conn.execute(
                """INSERT INTO project_workspaces (id,project_id,dir_path,canonical,status,created_at,meta_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (uid, project_id, dir_path, int(canonical), "active", now, "{}"),
            )
            self._conn.commit()
        return uid

    def promote_workspace(
        self,
        workspace_id: str,
        *,
        acted_by: str = "user",
        action_source: str = "inline_user_approval",
        reason: str = "",
    ) -> bool:
        """Mark this workspace as canonical, demote all others in same project."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT project_id, meta_json FROM project_workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE project_workspaces SET canonical=0 WHERE project_id=?",
                (row["project_id"],),
            )
            meta = json.loads(row["meta_json"] or "{}")
            meta["org_last_action_type"] = "promote_workspace"
            meta["org_last_action_by"] = acted_by
            meta["org_last_action_source"] = action_source
            meta["org_last_action_at"] = now
            meta["org_last_action_reason"] = reason or None
            self._conn.execute(
                "UPDATE project_workspaces SET canonical=1, promoted_at=?, meta_json=? WHERE id=?",
                (now, json.dumps(meta), workspace_id),
            )
            self._conn.commit()
        return True

    def archive_workspace(
        self,
        workspace_id: str,
        *,
        acted_by: str = "user",
        action_source: str = "inline_user_approval",
        reason: str = "",
    ) -> bool:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT meta_json FROM project_workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
            if not row:
                return False
            meta = json.loads(row["meta_json"] or "{}")
            meta["org_last_action_type"] = "archive_workspace"
            meta["org_last_action_by"] = acted_by
            meta["org_last_action_source"] = action_source
            meta["org_last_action_at"] = now
            meta["org_last_action_reason"] = reason or None
            cur = self._conn.execute(
                "UPDATE project_workspaces SET status='archived', meta_json=? WHERE id=?",
                (json.dumps(meta), workspace_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── Workspace reads ───────────────────────────────────────────────────────

    def get_workspace(self, workspace_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
        return _ws_row(row) if row else None

    def get_workspace_by_dir(self, dir_path: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_workspaces WHERE dir_path=?", (dir_path,)
            ).fetchone()
        return _ws_row(row) if row else None

    def get_workspaces(self, project_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM project_workspaces WHERE project_id=? ORDER BY canonical DESC, created_at",
                (project_id,),
            ).fetchall()
        return [_ws_row(r) for r in rows]

    # ── Journal ───────────────────────────────────────────────────────────────

    def add_journal_entry(self, node_id: str, content: str, session_id: str = "") -> str:
        uid = str(uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO node_journal_entries (id, node_id, session_id, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, node_id, session_id, content, now),
            )
            self._conn.commit()
        return uid

    def get_journal(self, node_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM node_journal_entries WHERE node_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (node_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_journal_by_topic_path(self, topic_path: str, limit: int = 20) -> list[dict]:
        """Get journal entries for node identified by topic_path."""
        node = self.get_by_topic_path(topic_path)
        if not node:
            return []
        return self.get_journal(node["id"], limit=limit)

    def dedupe_exact_nodes(
        self,
        *,
        limit_groups: int = 50,
        relink_node_reference: Callable[[str, str], None] | None = None,
    ) -> dict:
        """
        Collapse exact duplicate tree nodes by natural key.

        Key: (type, title, parent_id, topic_path, status)
        Keeps the oldest row as canonical and deletes newer duplicates after
        re-pointing journals, child parent_ids, and optional external references.
        """
        with self._lock:
            groups = self._conn.execute(
                """
                SELECT
                    type,
                    title,
                    COALESCE(parent_id, '') AS parent_id_key,
                    COALESCE(topic_path, '') AS topic_path_key,
                    status,
                    COUNT(*) AS duplicate_count
                FROM tree_nodes
                GROUP BY type, title, COALESCE(parent_id, ''), COALESCE(topic_path, ''), status
                HAVING COUNT(*) > 1
                ORDER BY duplicate_count DESC, MIN(created_at) ASC
                LIMIT ?
                """,
                (limit_groups,),
            ).fetchall()

            merged_groups = 0
            deleted_nodes = 0
            relinked_children = 0
            relinked_journals = 0
            canonical_ids: list[str] = []
            deleted_ids: list[str] = []

            for group in groups:
                rows = self._conn.execute(
                    """
                    SELECT * FROM tree_nodes
                    WHERE type = ?
                      AND title = ?
                      AND COALESCE(parent_id, '') = ?
                      AND COALESCE(topic_path, '') = ?
                      AND status = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (
                        group["type"],
                        group["title"],
                        group["parent_id_key"],
                        group["topic_path_key"],
                        group["status"],
                    ),
                ).fetchall()
                if len(rows) < 2:
                    continue

                canonical = _row(rows[0])
                canonical_id = str(canonical["id"])
                canonical_ids.append(canonical_id)

                merged_tags = list(canonical.get("tags") or [])
                merged_meta = dict(canonical.get("meta_json") or {})
                merged_description = str(canonical.get("description") or "")
                merged_goal = str(canonical.get("goal") or "")
                merged_doc = str(canonical.get("doc") or "")
                merged_doc_candidate = str(canonical.get("doc_candidate") or "")
                merged_doc_generated_at = canonical.get("doc_generated_at")
                merged_doc_candidate_generated_at = canonical.get("doc_candidate_generated_at")
                merged_done_at = canonical.get("done_at")

                loser_ids: list[str] = []
                for loser_row in rows[1:]:
                    loser = _row(loser_row)
                    loser_id = str(loser["id"])
                    loser_ids.append(loser_id)
                    deleted_ids.append(loser_id)
                    merged_tags = _merge_tags(merged_tags, loser.get("tags") or [])
                    merged_meta = _merge_meta_json(merged_meta, loser.get("meta_json") or {})
                    if not merged_description and loser.get("description"):
                        merged_description = str(loser["description"])
                    if not merged_goal and loser.get("goal"):
                        merged_goal = str(loser["goal"])
                    if not merged_doc and loser.get("doc"):
                        merged_doc = str(loser["doc"])
                    if not merged_doc_candidate and loser.get("doc_candidate"):
                        merged_doc_candidate = str(loser["doc_candidate"])
                    if merged_doc_generated_at is None and loser.get("doc_generated_at") is not None:
                        merged_doc_generated_at = loser.get("doc_generated_at")
                    if merged_doc_candidate_generated_at is None and loser.get("doc_candidate_generated_at") is not None:
                        merged_doc_candidate_generated_at = loser.get("doc_candidate_generated_at")
                    if merged_done_at is None and loser.get("done_at") is not None:
                        merged_done_at = loser.get("done_at")

                self._conn.execute(
                    """
                    UPDATE tree_nodes
                    SET description = ?,
                        goal = ?,
                        doc = ?,
                        doc_candidate = ?,
                        doc_generated_at = ?,
                        doc_candidate_generated_at = ?,
                        done_at = ?,
                        tags = ?,
                        meta_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        merged_description,
                        merged_goal,
                        merged_doc,
                        merged_doc_candidate,
                        merged_doc_generated_at,
                        merged_doc_candidate_generated_at,
                        merged_done_at,
                        json.dumps(merged_tags),
                        json.dumps(merged_meta),
                        time.time(),
                        canonical_id,
                    ),
                )

                for loser_id in loser_ids:
                    child_cur = self._conn.execute(
                        "UPDATE tree_nodes SET parent_id = ?, updated_at = ? WHERE parent_id = ?",
                        (canonical_id, time.time(), loser_id),
                    )
                    relinked_children += int(child_cur.rowcount or 0)
                    journal_cur = self._conn.execute(
                        "UPDATE node_journal_entries SET node_id = ? WHERE node_id = ?",
                        (canonical_id, loser_id),
                    )
                    relinked_journals += int(journal_cur.rowcount or 0)
                    if relink_node_reference is not None:
                        relink_node_reference(loser_id, canonical_id)
                    self._conn.execute("DELETE FROM tree_nodes WHERE id = ?", (loser_id,))
                    deleted_nodes += 1

                merged_groups += 1

            self._conn.commit()

        return {
            "merged_groups": merged_groups,
            "deleted_nodes": deleted_nodes,
            "relinked_children": relinked_children,
            "relinked_journals": relinked_journals,
            "canonical_ids": canonical_ids,
            "deleted_ids": deleted_ids,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[ProjectTreeStore] = None


def get_tree_store() -> ProjectTreeStore:
    global _store
    if _store is None:
        _store = ProjectTreeStore()
    return _store
