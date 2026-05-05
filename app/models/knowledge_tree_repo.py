import sqlite3
import os
from pathlib import Path
from threading import Lock
from datetime import datetime
from typing import Optional

from app.models.knowledge_tree import TreeNode, RoutingRule

# Размещаем БД рядом с остальными данными проекта
DEFAULT_DB_PATH = Path(os.getenv("KNOWLEDGE_TREE_DB_PATH", "qdrant_data/knowledge_tree.db"))

class KnowledgeTreeRepo:
    """Репозиторий для управления графом Дерева Знаний и правилами маршрутизации (SQLite)."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._init_db()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tree_nodes (
                path TEXT PRIMARY KEY,
                parent_path TEXT,
                level INTEGER NOT NULL,
                strength REAL DEFAULT 0.1,
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT NOT NULL,
                is_locked BOOLEAN DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS routing_rules (
                pattern TEXT PRIMARY KEY,
                slm_successes INTEGER DEFAULT 0,
                slm_failures INTEGER DEFAULT 0,
                requires_llm BOOLEAN DEFAULT 0
            );
        """)
        self._conn.commit()

    def get_node(self, path: str) -> Optional[TreeNode]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tree_nodes WHERE path = ?", (path,)).fetchone()
        if not row:
            return None
        return TreeNode(**dict(row))

    def upsert_node(self, node: TreeNode):
        with self._lock:
            self._conn.execute("""
                INSERT INTO tree_nodes (path, parent_path, level, strength, access_count, last_accessed, is_locked)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    strength = excluded.strength,
                    access_count = excluded.access_count,
                    last_accessed = excluded.last_accessed,
                    is_locked = excluded.is_locked
            """, (
                node.path, node.parent_path, node.level, node.strength,
                node.access_count, node.last_accessed.isoformat(), int(node.is_locked)
            ))
            self._conn.commit()

    def get_routing_rule(self, pattern: str) -> Optional[RoutingRule]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM routing_rules WHERE pattern = ?", (pattern,)).fetchone()
        if not row:
            return None
        return RoutingRule(**dict(row))

    def close(self):
        with self._lock:
            self._conn.close()

    def record_routing_success(self, pattern: str) -> None:
        """Фиксирует успешное совпадение SLM и LLM для паттерна запроса."""
        with self._lock:
            self._conn.execute("""
                INSERT INTO routing_rules (pattern, slm_successes, slm_failures, requires_llm)
                VALUES (?, 1, 0, 0)
                ON CONFLICT(pattern) DO UPDATE SET
                    slm_successes = slm_successes + 1
            """, (pattern,))
            self._conn.commit()

    def record_routing_failure(self, pattern: str, failure_threshold: float = 0.3, min_attempts: int = 3) -> None:
        """Фиксирует ошибку SLM. Если fail_rate > порога, переключает роутинг на LLM."""
        with self._lock:
            self._conn.execute("""
                INSERT INTO routing_rules (pattern, slm_successes, slm_failures, requires_llm)
                VALUES (?, 0, 1, 0)
                ON CONFLICT(pattern) DO UPDATE SET
                    slm_failures = slm_failures + 1
            """, (pattern,))
            
            row = self._conn.execute("SELECT slm_successes, slm_failures FROM routing_rules WHERE pattern = ?", (pattern,)).fetchone()
            if row:
                total = row["slm_successes"] + row["slm_failures"]
                if total >= min_attempts and (row["slm_failures"] / total) >= failure_threshold:
                    self._conn.execute("UPDATE routing_rules SET requires_llm = 1 WHERE pattern = ?", (pattern,))
                    
            self._conn.commit()
