from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Iterable, Optional

from app.models.project_task import ProjectTaskChangeRecord, ProjectTaskRecord
from app.services.system_data_root import data_path

_DB_PATH = data_path("project_tasks.db")

_CREATE_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS project_tasks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    topic_path TEXT,
    linked_improvement_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_tasks_project_status ON project_tasks(project, status);
CREATE INDEX IF NOT EXISTS idx_project_tasks_task_id ON project_tasks(project, task_id);
"""

_CREATE_CHANGES_SQL = """
CREATE TABLE IF NOT EXISTS project_task_changes (
    id TEXT PRIMARY KEY,
    memory_id TEXT,
    task_id TEXT NOT NULL,
    project TEXT NOT NULL,
    change_type TEXT NOT NULL,
    content TEXT NOT NULL,
    why TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    source TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_changes_project_task ON project_task_changes(project, task_id);
"""


def _tags_to_json(tags: Iterable[str] | None) -> str:
    return json.dumps(list(tags or []))


def _parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        if isinstance(data, list):
            return [str(item) for item in data if item is not None]
    except json.JSONDecodeError:
        pass
    return []


class ProjectTasksStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock:
            self._conn.executescript(_CREATE_TASKS_SQL)
            self._conn.executescript(_CREATE_CHANGES_SQL)
            self._conn.commit()

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        if "tags" in data:
            data["tags"] = _parse_tags(data.get("tags"))
        return data

    def upsert_task(
        self,
        *,
        memory_id: str,
        task_id: str,
        project: str,
        title: str,
        description: str,
        agent_id: str,
        status: str,
        source: str,
        tags: Iterable[str] | None = None,
        topic_path: str | None = None,
        linked_improvement_id: str | None = None,
        created_at: float,
        updated_at: float,
    ) -> None:
        tags_json = _tags_to_json(tags)
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM project_tasks WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if existing:
                created_at = existing["created_at"]
                self._conn.execute(
                    """
                    UPDATE project_tasks
                       SET task_id = ?, project = ?, title = ?, description = ?,
                           agent_id = ?, status = ?, source = ?, tags = ?,
                           topic_path = ?, linked_improvement_id = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        task_id,
                        project,
                        title,
                        description,
                        agent_id,
                        status,
                        source,
                        tags_json,
                        topic_path,
                        linked_improvement_id,
                        updated_at,
                        memory_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO project_tasks (
                        id, task_id, project, title, description, agent_id,
                        status, source, tags, topic_path, linked_improvement_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        task_id,
                        project,
                        title,
                        description,
                        agent_id,
                        status,
                        source,
                        tags_json,
                        topic_path,
                        linked_improvement_id,
                        created_at,
                        updated_at,
                    ),
                )
            self._conn.commit()

    def get_task(self, *, memory_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_tasks WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_task_by_task_id(self, *, project: str, task_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_tasks(
        self,
        *,
        project: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        updated_after: float | None = None,
        updated_before: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if created_after is not None:
            clauses.append("created_at >= ?")
            params.append(created_after)
        if created_before is not None:
            clauses.append("created_at <= ?")
            params.append(created_before)
        if updated_after is not None:
            clauses.append("updated_at >= ?")
            params.append(updated_after)
        if updated_before is not None:
            clauses.append("updated_at <= ?")
            params.append(updated_before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM project_tasks
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def add_change(
        self,
        *,
        memory_id: str,
        task_id: str,
        project: str,
        change_type: str,
        content: str,
        why: str,
        agent_id: str,
        source: str,
        tags: Iterable[str] | None = None,
        created_at: float,
    ) -> None:
        tags_json = _tags_to_json(tags)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO project_task_changes (
                    id, memory_id, task_id, project, change_type, content, why,
                    agent_id, source, tags, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    memory_id,
                    task_id,
                    project,
                    change_type,
                    content,
                    why,
                    agent_id,
                    source,
                    tags_json,
                    created_at,
                ),
            )
            self._conn.commit()

    def list_changes(
        self,
        *,
        project: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM project_task_changes
                {where}
                ORDER BY created_at ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def delete_task(self, *, memory_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM project_tasks WHERE id = ?",
                (memory_id,),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORE: Optional[ProjectTasksStore] = None


def get_project_tasks_store() -> ProjectTasksStore:
    global _STORE
    if _STORE is None:
        _STORE = ProjectTasksStore()
    return _STORE


def close_project_tasks_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None
