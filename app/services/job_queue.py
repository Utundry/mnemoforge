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
import os
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
    lane         TEXT NOT NULL DEFAULT 'fast',
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

_SLOW_JOB_TYPES = {
    "skills_retag",
    "qdrant_reindex_from_sqlite",
    "project_ingest",
    "project_refresh",
    "rebuild_project_tasks",
    "docs_rebuild",
    "docs_sync_memory",
    "regenerate_skill_content",
    "evolve_skills",
    "verify_tree_classification",
    "memory_scribe_compact",
    "draft_task_checkpoint",
}


class JobQueue:
    def __init__(self, db_path: Path) -> None:
        total_workers = max(1, int(os.getenv("JOB_QUEUE_WORKERS", "2")))
        fast_workers = os.getenv("JOB_QUEUE_FAST_WORKERS")
        slow_workers = os.getenv("JOB_QUEUE_SLOW_WORKERS")
        if fast_workers not in {None, ""} or slow_workers not in {None, ""}:
            resolved_fast_workers = max(1, int(fast_workers or "1"))
            resolved_slow_workers = max(1, int(slow_workers or "1"))
        elif total_workers <= 1:
            resolved_fast_workers = 1
            resolved_slow_workers = 1
        else:
            resolved_slow_workers = 1
            resolved_fast_workers = max(1, total_workers - resolved_slow_workers)
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "lane" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'fast'")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_lane_status ON jobs(lane, status, created_at)")
        self._conn.commit()
        self._fast_queue: asyncio.Queue = asyncio.Queue()
        self._slow_queue: asyncio.Queue = asyncio.Queue()
        self._handlers: dict[str, Handler] = {}
        self._fast_worker_count = resolved_fast_workers
        self._slow_worker_count = resolved_slow_workers
        self._worker_task: Optional[asyncio.Task] = None
        self._worker_tasks: list[asyncio.Task] = []
        logger.info(
            "JobQueue initialized: %s (fast_workers=%d slow_workers=%d)",
            db_path,
            resolved_fast_workers,
            resolved_slow_workers,
        )

    # ── Handler registry ────────────────────────────────────────────────────────

    def register(self, job_type: str, handler: Handler) -> None:
        """Register an async handler for a job type."""
        self._handlers[job_type] = handler
        logger.debug("Registered handler for job_type '%s'", job_type)

    # ── Submit ──────────────────────────────────────────────────────────────────

    def _resolve_lane(self, job_type: str, payload: dict[str, Any] | None = None) -> str:
        requested = str((payload or {}).get("_queue_lane") or "").strip().lower()
        if requested in {"fast", "slow"}:
            return requested
        return "slow" if job_type in _SLOW_JOB_TYPES else "fast"

    async def _enqueue_job(self, job_id: str, lane: str) -> None:
        if lane == "slow":
            await self._slow_queue.put(job_id)
            return
        await self._fast_queue.put(job_id)

    async def submit(self, job_type: str, payload: dict) -> str:
        """Persist job and enqueue for background processing. Returns job_id."""
        job_id = str(uuid.uuid4())
        now = time.time()
        lane = self._resolve_lane(job_type, payload)
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(id, job_type, lane, payload, status, created_at) VALUES (?,?,?,?,?,?)",
                (job_id, job_type, lane, json.dumps(payload), "queued", now),
            )
            self._conn.commit()
        await self._enqueue_job(job_id, lane)
        logger.info("Submitted job %s type=%s lane=%s", job_id[:8], job_type, lane)
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
        lane: Optional[str] = None,
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
        if lane:
            sql += " AND lane=?"
            params.append(lane)
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

    async def _worker_loop(self, queue: asyncio.Queue, *, lane: str, worker_index: int = 0) -> None:
        logger.info("Job queue worker started (lane=%s worker=%d)", lane, worker_index)
        while True:
            try:
                job_id = await queue.get()
                await self._process(job_id)
                queue.task_done()
            except asyncio.CancelledError:
                logger.info("Job queue worker stopped (lane=%s worker=%d)", lane, worker_index)
                break
            except Exception as e:
                logger.error("Unexpected worker error: %s", e)

    async def start(self) -> None:
        """Re-queue interrupted jobs and start the background worker."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, lane FROM jobs WHERE status IN ('queued','running') ORDER BY created_at"
            ).fetchall()
            # Jobs stuck in 'running' were interrupted by server restart → re-queue
            self._conn.execute(
                "UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'"
            )
            self._conn.commit()

        for row in rows:
            await self._enqueue_job(row["id"], str(row["lane"] or "fast"))
        if rows:
            logger.info("Re-queued %d interrupted jobs", len(rows))

        self._worker_tasks = []
        self._worker_tasks.extend(
            asyncio.create_task(self._worker_loop(self._fast_queue, lane="fast", worker_index=worker_index + 1))
            for worker_index in range(self._fast_worker_count)
        )
        self._worker_tasks.extend(
            asyncio.create_task(self._worker_loop(self._slow_queue, lane="slow", worker_index=worker_index + 1))
            for worker_index in range(self._slow_worker_count)
        )
        self._worker_task = self._worker_tasks[0] if self._worker_tasks else None

    async def stop(self) -> None:
        for task in self._worker_tasks:
            task.cancel()
        for task in self._worker_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker_tasks = []
        self._worker_task = None
        with self._lock:
            self._conn.close()


# ── Singleton ────────────────────────────────────────────────────────────────

_queue: Optional[JobQueue] = None


def get_job_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue(Path("qdrant_data") / "jobs.db")
    return _queue
