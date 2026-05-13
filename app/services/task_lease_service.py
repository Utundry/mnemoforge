from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

from app.models.task_lease import TaskLeaseClaimResult, TaskLeaseRecord


_DB_PATH = Path("qdrant_data") / "task_leases.db"
DEFAULT_LEASE_TTL_SECONDS = 15 * 60

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS task_leases (
    lease_id            TEXT PRIMARY KEY,
    project             TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    owner_agent         TEXT NOT NULL,
    session_id          TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,
    claimed_at          REAL NOT NULL,
    heartbeat_at        REAL NOT NULL,
    expires_at          REAL NOT NULL,
    released_at         REAL,
    release_reason      TEXT NOT NULL DEFAULT '',
    lease_ttl_seconds   INTEGER NOT NULL,
    previous_lease_id   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_leases_project_task_status
    ON task_leases(project, task_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_task_leases_owner_session
    ON task_leases(owner_agent, session_id, status, heartbeat_at);
"""


class TaskLeaseConflict(ValueError):
    def __init__(self, active_lease: TaskLeaseRecord) -> None:
        super().__init__(
            f"Task {active_lease.project}/{active_lease.task_id} is already claimed by {active_lease.owner_agent}."
        )
        self.active_lease = active_lease

    def to_dict(self) -> dict:
        return {
            "error": "task_already_claimed",
            "message": str(self),
            "active_lease": self.active_lease.model_dump(mode="json"),
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime | None = None) -> float:
    return (value or _utcnow()).timestamp()


def _dt(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _clean_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


class TaskLeaseStore:
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

    def _row_to_lease(self, row: sqlite3.Row) -> TaskLeaseRecord:
        return TaskLeaseRecord(
            lease_id=str(row["lease_id"]),
            project=str(row["project"]),
            task_id=str(row["task_id"]),
            owner_agent=str(row["owner_agent"]),
            session_id=str(row["session_id"] or ""),
            status=str(row["status"]),
            claimed_at=_dt(row["claimed_at"]) or _utcnow(),
            heartbeat_at=_dt(row["heartbeat_at"]) or _utcnow(),
            expires_at=_dt(row["expires_at"]) or _utcnow(),
            released_at=_dt(row["released_at"]),
            release_reason=str(row["release_reason"] or ""),
            lease_ttl_seconds=int(row["lease_ttl_seconds"] or DEFAULT_LEASE_TTL_SECONDS),
            previous_lease_id=str(row["previous_lease_id"] or ""),
        )

    def expire_stale(self, *, now: datetime | None = None) -> list[TaskLeaseRecord]:
        now_ts = _ts(now)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM task_leases
                 WHERE status = 'active' AND expires_at <= ?
                 ORDER BY expires_at ASC
                """,
                (now_ts,),
            ).fetchall()
            if rows:
                lease_ids = [str(row["lease_id"]) for row in rows]
                self._conn.execute(
                    """
                    UPDATE task_leases
                       SET status = 'expired',
                           released_at = ?,
                           release_reason = CASE
                               WHEN release_reason = '' THEN 'lease_timeout'
                               ELSE release_reason
                           END
                     WHERE status = 'active' AND expires_at <= ?
                    """,
                    (now_ts, now_ts),
                )
                self._conn.commit()
                placeholders = ", ".join("?" for _ in lease_ids)
                rows = self._conn.execute(
                    f"SELECT * FROM task_leases WHERE lease_id IN ({placeholders}) ORDER BY expires_at ASC",
                    lease_ids,
                ).fetchall()
        return [self._row_to_lease(row) for row in rows]

    def get_active_claim(self, *, project: str, task_id: str, now: datetime | None = None) -> TaskLeaseRecord | None:
        project = _clean_text(project, 128)
        task_id = _clean_text(task_id, 128)
        if not project or not task_id:
            return None
        self.expire_stale(now=now)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM task_leases
                 WHERE project = ? AND task_id = ? AND status = 'active'
                 ORDER BY heartbeat_at DESC
                 LIMIT 1
                """,
                (project, task_id),
            ).fetchone()
        return self._row_to_lease(row) if row else None

    def claim(
        self,
        *,
        project: str,
        task_id: str,
        owner_agent: str,
        session_id: str = "",
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        now: datetime | None = None,
        allow_reentrant: bool = True,
    ) -> TaskLeaseClaimResult:
        project = _clean_text(project, 128)
        task_id = _clean_text(task_id, 128)
        owner_agent = _clean_text(owner_agent, 128)
        session_id = _clean_text(session_id, 256)
        if not project or not task_id or not owner_agent:
            raise ValueError("project, task_id, and owner_agent are required")

        ttl = max(5, int(lease_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS))
        now_dt = now or _utcnow()
        now_ts = _ts(now_dt)
        expires_ts = _ts(now_dt + timedelta(seconds=ttl))
        expired = self.expire_stale(now=now_dt)
        previous = expired[-1] if expired else None

        with self._lock:
            active_row = self._conn.execute(
                """
                SELECT * FROM task_leases
                 WHERE project = ? AND task_id = ? AND status = 'active'
                 ORDER BY heartbeat_at DESC
                 LIMIT 1
                """,
                (project, task_id),
            ).fetchone()
            if active_row:
                active = self._row_to_lease(active_row)
                same_owner = active.owner_agent == owner_agent and (not session_id or active.session_id == session_id)
                if allow_reentrant and same_owner:
                    self._conn.execute(
                        """
                        UPDATE task_leases
                           SET heartbeat_at = ?, expires_at = ?, lease_ttl_seconds = ?
                         WHERE lease_id = ?
                        """,
                        (now_ts, expires_ts, ttl, active.lease_id),
                    )
                    self._conn.commit()
                    refreshed = self._conn.execute(
                        "SELECT * FROM task_leases WHERE lease_id = ?",
                        (active.lease_id,),
                    ).fetchone()
                    return TaskLeaseClaimResult(status="renewed", lease=self._row_to_lease(refreshed))
                raise TaskLeaseConflict(active)

            lease_id = str(uuid4())
            self._conn.execute(
                """
                INSERT INTO task_leases (
                    lease_id, project, task_id, owner_agent, session_id, status,
                    claimed_at, heartbeat_at, expires_at, released_at,
                    release_reason, lease_ttl_seconds, previous_lease_id
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, '', ?, ?)
                """,
                (
                    lease_id,
                    project,
                    task_id,
                    owner_agent,
                    session_id,
                    now_ts,
                    now_ts,
                    expires_ts,
                    ttl,
                    previous.lease_id if previous else "",
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)).fetchone()
        return TaskLeaseClaimResult(
            status="claimed",
            lease=self._row_to_lease(row),
            previous_claim_expired=previous is not None,
            previous_lease=previous,
        )

    def heartbeat(
        self,
        *,
        lease_id: str,
        owner_agent: str,
        session_id: str = "",
        lease_ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> TaskLeaseRecord:
        lease_id = _clean_text(lease_id, 128)
        owner_agent = _clean_text(owner_agent, 128)
        session_id = _clean_text(session_id, 256)
        now_dt = now or _utcnow()
        self.expire_stale(now=now_dt)
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row:
                raise ValueError("lease_not_found")
            lease = self._row_to_lease(row)
            if lease.status != "active":
                raise ValueError(f"lease_not_active:{lease.status}")
            if lease.owner_agent != owner_agent or (session_id and lease.session_id != session_id):
                raise PermissionError("lease_owner_mismatch")
            ttl = max(5, int(lease_ttl_seconds or lease.lease_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS))
            now_ts = _ts(now_dt)
            expires_ts = _ts(now_dt + timedelta(seconds=ttl))
            self._conn.execute(
                """
                UPDATE task_leases
                   SET heartbeat_at = ?, expires_at = ?, lease_ttl_seconds = ?
                 WHERE lease_id = ?
                """,
                (now_ts, expires_ts, ttl, lease_id),
            )
            self._conn.commit()
            updated = self._conn.execute("SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)).fetchone()
        return self._row_to_lease(updated)

    def release(
        self,
        *,
        lease_id: str,
        owner_agent: str,
        session_id: str = "",
        reason: str = "released",
        status: str = "released",
        now: datetime | None = None,
    ) -> TaskLeaseRecord:
        if status not in {"released", "transferred", "expired"}:
            raise ValueError("release status must be released, transferred, or expired")
        lease_id = _clean_text(lease_id, 128)
        owner_agent = _clean_text(owner_agent, 128)
        session_id = _clean_text(session_id, 256)
        released_ts = _ts(now)
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row:
                raise ValueError("lease_not_found")
            lease = self._row_to_lease(row)
            if lease.owner_agent != owner_agent or (session_id and lease.session_id != session_id):
                raise PermissionError("lease_owner_mismatch")
            if lease.status != "active":
                return lease
            self._conn.execute(
                """
                UPDATE task_leases
                   SET status = ?, released_at = ?, release_reason = ?
                 WHERE lease_id = ?
                """,
                (status, released_ts, _clean_text(reason, 256) or status, lease_id),
            )
            self._conn.commit()
            updated = self._conn.execute("SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)).fetchone()
        return self._row_to_lease(updated)

    def list_leases(
        self,
        *,
        project: str | None = None,
        task_id: str | None = None,
        owner_agent: str | None = None,
        status: str | None = None,
        include_expired_update: bool = True,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[TaskLeaseRecord]:
        if include_expired_update:
            self.expire_stale(now=now)
        clauses: list[str] = []
        params: list[object] = []
        if project:
            clauses.append("project = ?")
            params.append(_clean_text(project, 128))
        if task_id:
            clauses.append("task_id = ?")
            params.append(_clean_text(task_id, 128))
        if owner_agent:
            clauses.append("owner_agent = ?")
            params.append(_clean_text(owner_agent, 128))
        if status and status != "all":
            clauses.append("status = ?")
            params.append(_clean_text(status, 32))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(500, int(limit or 50))))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM task_leases
                {where}
                ORDER BY heartbeat_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_lease(row) for row in rows]


class TaskLeaseHeartbeatHandle:
    def __init__(
        self,
        *,
        store: TaskLeaseStore,
        lease: TaskLeaseRecord,
        heartbeat_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.lease = lease
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds or 30.0))
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> "TaskLeaseHeartbeatHandle":
        self._thread = Thread(target=self._heartbeat_loop, name="task-lease-heartbeat", daemon=True)
        self._thread.start()
        return self

    def close(self, *, release: bool = False, reason: str = "heartbeat_handle_closed") -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if release:
            try:
                self.lease = self.store.release(
                    lease_id=self.lease.lease_id,
                    owner_agent=self.lease.owner_agent,
                    session_id=self.lease.session_id,
                    reason=reason,
                )
            except Exception:
                pass

    def heartbeat_once(self, *, now: datetime | None = None) -> TaskLeaseRecord:
        self.lease = self.store.heartbeat(
            lease_id=self.lease.lease_id,
            owner_agent=self.lease.owner_agent,
            session_id=self.lease.session_id,
            now=now,
        )
        return self.lease

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.heartbeat_once()
            except Exception:
                return


def acquire_task_lease_with_heartbeat(
    *,
    store: TaskLeaseStore,
    project: str,
    task_id: str,
    owner_agent: str,
    session_id: str = "",
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    heartbeat_seconds: float | None = None,
    now: datetime | None = None,
) -> tuple[TaskLeaseClaimResult, TaskLeaseHeartbeatHandle]:
    result = store.claim(
        project=project,
        task_id=task_id,
        owner_agent=owner_agent,
        session_id=session_id,
        lease_ttl_seconds=lease_ttl_seconds,
        now=now,
    )
    interval = heartbeat_seconds if heartbeat_seconds is not None else max(1.0, lease_ttl_seconds / 3)
    return result, TaskLeaseHeartbeatHandle(store=store, lease=result.lease, heartbeat_seconds=interval).start()


_STORE: TaskLeaseStore | None = None


def get_task_lease_store() -> TaskLeaseStore:
    global _STORE
    if _STORE is None:
        _STORE = TaskLeaseStore()
    return _STORE


def close_task_lease_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None
