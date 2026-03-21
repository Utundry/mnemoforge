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
from pathlib import Path
from threading import Lock
from typing import Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

_DB_PATH = Path("qdrant_data") / "project_tree.db"

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
    promoted_at  REAL
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
    return d


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
        ]:
            try:
                self._conn.execute(f"ALTER TABLE tree_nodes ADD COLUMN {col} {definition}")
                logger.info("Migration: added column %s to tree_nodes", col)
            except sqlite3.OperationalError:
                pass  # column already exists
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
                   "doc", "doc_candidate", "doc_generated_at", "tags", "sort_order",
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
                """INSERT INTO project_workspaces (id,project_id,dir_path,canonical,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (uid, project_id, dir_path, int(canonical), "active", now),
            )
            self._conn.commit()
        return uid

    def promote_workspace(self, workspace_id: str) -> bool:
        """Mark this workspace as canonical, demote all others in same project."""
        with self._lock:
            row = self._conn.execute(
                "SELECT project_id FROM project_workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE project_workspaces SET canonical=0 WHERE project_id=?",
                (row["project_id"],),
            )
            self._conn.execute(
                "UPDATE project_workspaces SET canonical=1, promoted_at=? WHERE id=?",
                (time.time(), workspace_id),
            )
            self._conn.commit()
        return True

    def archive_workspace(self, workspace_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE project_workspaces SET status='archived' WHERE id=?", (workspace_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── Workspace reads ───────────────────────────────────────────────────────

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
