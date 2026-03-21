"""
SQLite-backed async job queue for LLM-heavy background operations.

Problem: local LLM (qwen3:1.7b) can take 30-120s per request. Synchronous endpoints
time out on the client side. This queue decouples submission from execution.

Pattern:
  POST /project/ingest?background=true  → { job_id, status: "queued" }
  GET  /tasks/{job_id}                  → { status: "done", result: {...} }

Handlers are registered by job_type after services are initialized in lifespan.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    job_type     TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    result       TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type    ON jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""

Handler = Callable[[dict], Coroutine[Any, Any, dict]]


class JobQueue:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._handlers: dict[str, Handler] = {}
        self._worker_task: Optional[asyncio.Task] = None
        logger.info("JobQueue initialized: %s", db_path)

    # ── Handler registry ────────────────────────────────────────────────────────

    def register(self, job_type: str, handler: Handler) -> None:
        """Register an async handler for a job type."""
        self._handlers[job_type] = handler
        logger.debug("Registered handler for job_type '%s'", job_type)

    # ── Submit ──────────────────────────────────────────────────────────────────

    async def submit(self, job_type: str, payload: dict) -> str:
        """Persist job and enqueue for background processing. Returns job_id."""
        job_id = str(uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(id, job_type, payload, status, created_at) VALUES (?,?,?,?,?)",
                (job_id, job_type, json.dumps(payload), "queued", now),
            )
            self._conn.commit()
        await self._queue.put(job_id)
        logger.info("Submitted job %s type=%s", job_id[:8], job_type)
        return job_id

    # ── Query ───────────────────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_jobs(
        self,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if job_type:
            sql += " AND job_type=?"
            params.append(job_type)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        for field in ("payload", "result"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        return d

    # ── Worker ──────────────────────────────────────────────────────────────────

    def _set_running(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?",
                (time.time(), job_id),
            )
            self._conn.commit()

    def _set_done(self, job_id: str, result: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='done', finished_at=?, result=? WHERE id=?",
                (time.time(), json.dumps(result), job_id),
            )
            self._conn.commit()

    def _set_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                (time.time(), error, job_id),
            )
            self._conn.commit()

    async def _process(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job or job["status"] != "queued":
            return

        job_type = job["job_type"]
        handler = self._handlers.get(job_type)
        if not handler:
            self._set_failed(job_id, f"No handler registered for job_type '{job_type}'")
            logger.error("No handler for job_type '%s'", job_type)
            return

        self._set_running(job_id)
        logger.info("Processing job %s type=%s", job_id[:8], job_type)
        try:
            result = await handler(job["payload"])
            self._set_done(job_id, result)
            logger.info("Job %s done: %s", job_id[:8], job_type)
        except Exception as e:
            self._set_failed(job_id, str(e))
            logger.error("Job %s failed (%s): %s", job_id[:8], job_type, e)

    async def _worker_loop(self) -> None:
        logger.info("Job queue worker started")
        while True:
            try:
                job_id = await self._queue.get()
                await self._process(job_id)
                self._queue.task_done()
            except asyncio.CancelledError:
                logger.info("Job queue worker stopped")
                break
            except Exception as e:
                logger.error("Unexpected worker error: %s", e)

    async def start(self) -> None:
        """Re-queue interrupted jobs and start the background worker."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','running') ORDER BY created_at"
            ).fetchall()
            # Jobs stuck in 'running' were interrupted by server restart → re-queue
            self._conn.execute(
                "UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'"
            )
            self._conn.commit()

        for row in rows:
            await self._queue.put(row[0])
        if rows:
            logger.info("Re-queued %d interrupted jobs", len(rows))

        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        with self._lock:
            self._conn.close()


# ── Singleton ────────────────────────────────────────────────────────────────

_queue: Optional[JobQueue] = None


def get_job_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue(Path("qdrant_data") / "jobs.db")
    return _queue
