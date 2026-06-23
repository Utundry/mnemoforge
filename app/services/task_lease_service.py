from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

from app.models.task_lease import TaskLeaseClaimResult, TaskLeaseRecord
from app.services.system_data_root import data_path


_DB_PATH = data_path("task_leases.db")
DEFAULT_LEASE_TTL_SECONDS = 15 * 60
_WORK_TOKEN_BYTES = 32
_WORK_TOKEN_PREVIEW_LEN = 8

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS task_leases (
    lease_id            TEXT PRIMARY KEY,
    project             TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    owner_agent         TEXT NOT NULL,
    session_id          TEXT NOT NULL DEFAULT '',
    agent_fingerprint   TEXT NOT NULL DEFAULT '',
    runtime_profile_id  TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,
    claimed_at          REAL NOT NULL,
    heartbeat_at        REAL NOT NULL,
    expires_at          REAL NOT NULL,
    released_at         REAL,
    release_reason      TEXT NOT NULL DEFAULT '',
    lease_ttl_seconds   INTEGER NOT NULL,
    previous_lease_id   TEXT NOT NULL DEFAULT '',
    work_token_hash     TEXT NOT NULL DEFAULT '',
    work_token_preview  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_leases_project_task_status
    ON task_leases(project, task_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_task_leases_owner_session
    ON task_leases(owner_agent, session_id, status, heartbeat_at);
"""

_MIGRATE_SQL = [
    "ALTER TABLE task_leases ADD COLUMN agent_fingerprint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE task_leases ADD COLUMN runtime_profile_id TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_task_leases_fingerprint ON task_leases(agent_fingerprint, project, task_id, status, heartbeat_at)",
]


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


class TaskLeaseUnavailable(ValueError):
    def __init__(self, lease: TaskLeaseRecord, *, reason: str) -> None:
        super().__init__(f"Task lease {lease.lease_id} is not active: {lease.status}.")
        self.lease = lease
        self.reason = reason

    def to_dict(self) -> dict:
        return {
            "error": self.reason,
            "message": str(self),
            "lease": self.lease.model_dump(mode="json"),
        }


class WorkTokenMismatch(PermissionError):
    def __init__(self, lease_id: str) -> None:
        super().__init__(f"Work token mismatch for lease {lease_id}.")
        self.lease_id = lease_id


def _generate_work_token() -> str:
    return secrets.token_hex(_WORK_TOKEN_BYTES)


def _hash_work_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _work_token_preview(token: str) -> str:
    return token[:_WORK_TOKEN_PREVIEW_LEN]


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
            self._migrate()

    def _migrate(self) -> None:
        for sql in _MIGRATE_SQL:
            try:
                self._conn.execute(sql)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    def close(self) -> None:
        stop_task_lease_auto_heartbeats_for_store(self)
        with self._lock:
            self._conn.close()

    def _row_to_lease(self, row: sqlite3.Row) -> TaskLeaseRecord:
        return TaskLeaseRecord(
            lease_id=str(row["lease_id"]),
            project=str(row["project"]),
            task_id=str(row["task_id"]),
            owner_agent=str(row["owner_agent"]),
            session_id=str(row["session_id"] or ""),
            agent_fingerprint=str(row["agent_fingerprint"] or ""),
            runtime_profile_id=str(row["runtime_profile_id"] or ""),
            status=str(row["status"]),
            claimed_at=_dt(row["claimed_at"]) or _utcnow(),
            heartbeat_at=_dt(row["heartbeat_at"]) or _utcnow(),
            expires_at=_dt(row["expires_at"]) or _utcnow(),
            released_at=_dt(row["released_at"]),
            release_reason=str(row["release_reason"] or ""),
            lease_ttl_seconds=int(row["lease_ttl_seconds"] or DEFAULT_LEASE_TTL_SECONDS),
            previous_lease_id=str(row["previous_lease_id"] or ""),
            work_token_hash=str(row["work_token_hash"] or ""),
            work_token_preview=str(row["work_token_preview"] or ""),
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
        agent_fingerprint: str = "",
        runtime_profile_id: str = "",
        work_token: str = "",
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        now: datetime | None = None,
        allow_reentrant: bool = True,
    ) -> TaskLeaseClaimResult:
        project = _clean_text(project, 128)
        task_id = _clean_text(task_id, 128)
        owner_agent = _clean_text(owner_agent, 128)
        session_id = _clean_text(session_id, 256)
        agent_fingerprint = _clean_text(agent_fingerprint, 256)
        runtime_profile_id = _clean_text(runtime_profile_id, 128)
        work_token = str(work_token or "").strip()
        if not project or not task_id or not owner_agent:
            raise ValueError("project, task_id, and owner_agent are required")
        if not session_id:
            raise ValueError("session_id is required")

        ttl = max(5, int(lease_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS))
        now_dt = now or _utcnow()
        now_ts = _ts(now_dt)
        expires_ts = _ts(now_dt + timedelta(seconds=ttl))
        self.expire_stale(now=now_dt)

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
                same_owner = active.owner_agent == owner_agent and active.session_id == session_id
                if allow_reentrant and same_owner:
                    renewed_work_token = _generate_work_token()
                    self._conn.execute(
                        """
                        UPDATE task_leases
                           SET heartbeat_at = ?,
                               expires_at = ?,
                               lease_ttl_seconds = ?,
                               work_token_hash = ?,
                               work_token_preview = ?
                         WHERE lease_id = ?
                        """,
                        (
                            now_ts,
                            expires_ts,
                            ttl,
                            _hash_work_token(renewed_work_token),
                            _work_token_preview(renewed_work_token),
                            active.lease_id,
                        ),
                    )
                    self._conn.commit()
                    refreshed = self._conn.execute(
                        "SELECT * FROM task_leases WHERE lease_id = ?",
                        (active.lease_id,),
                    ).fetchone()
                    return TaskLeaseClaimResult(
                        status="renewed",
                        lease=self._row_to_lease(refreshed),
                        work_token=renewed_work_token,
                    )
                same_fingerprint = bool(
                    agent_fingerprint
                    and active.agent_fingerprint
                    and active.agent_fingerprint == agent_fingerprint
                )
                token_valid = bool(
                    work_token
                    and active.work_token_hash
                    and active.work_token_hash == _hash_work_token(work_token)
                )
                if same_fingerprint and work_token and not token_valid:
                    raise WorkTokenMismatch(active.lease_id)
                if same_fingerprint and token_valid:
                    self._conn.execute(
                        """
                        UPDATE task_leases
                           SET owner_agent = ?,
                               session_id = ?,
                               runtime_profile_id = ?,
                               heartbeat_at = ?,
                               expires_at = ?,
                               lease_ttl_seconds = ?
                         WHERE lease_id = ?
                        """,
                        (
                            owner_agent,
                            session_id,
                            runtime_profile_id or active.runtime_profile_id,
                            now_ts,
                            expires_ts,
                            ttl,
                            active.lease_id,
                        ),
                    )
                    self._conn.commit()
                    refreshed = self._conn.execute(
                        "SELECT * FROM task_leases WHERE lease_id = ?",
                        (active.lease_id,),
                    ).fetchone()
                    return TaskLeaseClaimResult(
                        status="reclaimed",
                        lease=self._row_to_lease(refreshed),
                        same_fingerprint_reclaim=True,
                        previous_lease=active,
                        work_token=work_token,
                    )
                raise TaskLeaseConflict(active)

            previous_row = self._conn.execute(
                """
                SELECT * FROM task_leases
                 WHERE project = ? AND task_id = ? AND status != 'active'
                 ORDER BY heartbeat_at DESC
                 LIMIT 1
                """,
                (project, task_id),
            ).fetchone()
            previous = self._row_to_lease(previous_row) if previous_row else None
            same_fingerprint_reclaim = bool(
                previous
                and agent_fingerprint
                and previous.agent_fingerprint
                and previous.agent_fingerprint == agent_fingerprint
            )
            lease_id = str(uuid4())
            work_token = _generate_work_token()
            work_token_hash = _hash_work_token(work_token)
            self._conn.execute(
                """
                INSERT INTO task_leases (
                    lease_id, project, task_id, owner_agent, session_id,
                    agent_fingerprint, runtime_profile_id, status,
                    claimed_at, heartbeat_at, expires_at, released_at,
                    release_reason, lease_ttl_seconds, previous_lease_id,
                    work_token_hash, work_token_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, '', ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    project,
                    task_id,
                    owner_agent,
                    session_id,
                    agent_fingerprint,
                    runtime_profile_id,
                    now_ts,
                    now_ts,
                    expires_ts,
                    ttl,
                    previous.lease_id if previous else "",
                    work_token_hash,
                    _work_token_preview(work_token),
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)).fetchone()
        return TaskLeaseClaimResult(
            status="reclaimed" if same_fingerprint_reclaim else "claimed",
            lease=self._row_to_lease(row),
            previous_claim_expired=previous is not None,
            same_fingerprint_reclaim=same_fingerprint_reclaim,
            previous_lease=previous,
            work_token=work_token,
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
                raise TaskLeaseUnavailable(
                    lease,
                    reason="lease_expired" if lease.status == "expired" else f"lease_not_active:{lease.status}",
                )
            if lease.owner_agent != owner_agent or lease.session_id != session_id:
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
            if lease.owner_agent != owner_agent or lease.session_id != session_id:
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

    def force_release(
        self,
        *,
        lease_id: str,
        acted_by: str,
        reason: str = "force_released",
        status: str = "released",
        now: datetime | None = None,
    ) -> TaskLeaseRecord:
        if status not in {"released", "transferred", "expired"}:
            raise ValueError("release status must be released, transferred, or expired")
        lease_id = _clean_text(lease_id, 128)
        acted_by = _clean_text(acted_by, 128)
        if not acted_by:
            raise ValueError("acted_by is required")
        released_ts = _ts(now)
        reason_clean = _clean_text(reason, 256) or status
        audit_reason = _clean_text(f"force_release:{acted_by}:{reason_clean}", 256)
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row:
                raise ValueError("lease_not_found")
            lease = self._row_to_lease(row)
            if lease.status != "active":
                return lease
            self._conn.execute(
                """
                UPDATE task_leases
                   SET status = ?, released_at = ?, release_reason = ?
                 WHERE lease_id = ?
                """,
                (status, released_ts, audit_reason, lease_id),
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
        agent_fingerprint: str | None = None,
        runtime_profile_id: str | None = None,
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
        if agent_fingerprint:
            clauses.append("agent_fingerprint = ?")
            params.append(_clean_text(agent_fingerprint, 256))
        if runtime_profile_id:
            clauses.append("runtime_profile_id = ?")
            params.append(_clean_text(runtime_profile_id, 128))
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

    def verify_work_token(self, *, lease_id: str, work_token: str) -> bool:
        """Verify a work_token against the stored hash. Only works for active leases."""
        lease_id = _clean_text(lease_id, 128)
        work_token = str(work_token or "").strip()
        if not lease_id or not work_token:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT work_token_hash, status FROM task_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        if not row:
            return False
        if str(row["status"]) != "active":
            return False
        stored_hash = str(row["work_token_hash"] or "")
        if not stored_hash:
            return False
        computed = _hash_work_token(work_token)
        return stored_hash == computed

    def latest_continuity_lease(
        self,
        *,
        project: str,
        task_id: str,
        owner_agent: str,
        session_id: str = "",
        work_token: str = "",
    ) -> TaskLeaseRecord | None:
        """Find the latest non-active lease that proves same-owner continuity."""
        project = _clean_text(project, 128)
        task_id = _clean_text(task_id, 128)
        owner_agent = _clean_text(owner_agent, 128)
        session_id = _clean_text(session_id, 256)
        work_token = str(work_token or "").strip()
        if not project or not task_id or not owner_agent or not session_id or not work_token:
            return None
        token_hash = _hash_work_token(work_token)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM task_leases
                 WHERE project = ? AND task_id = ? AND owner_agent = ? AND session_id = ?
                   AND status != 'active'
                 ORDER BY heartbeat_at DESC
                 LIMIT 1
                """,
                (project, task_id, owner_agent, session_id),
            ).fetchone()
        if not row:
            return None
        lease = self._row_to_lease(row)
        if not lease.work_token_hash or lease.work_token_hash != token_hash:
            return None
        return lease

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


_HEARTBEATS: dict[str, TaskLeaseHeartbeatHandle] = {}


def start_task_lease_auto_heartbeat(
    *,
    store: TaskLeaseStore,
    lease: TaskLeaseRecord,
    heartbeat_seconds: float | None = None,
) -> TaskLeaseHeartbeatHandle:
    interval = heartbeat_seconds if heartbeat_seconds is not None else max(1.0, lease.lease_ttl_seconds / 3)
    stop_task_lease_auto_heartbeat(lease.lease_id)
    handle = TaskLeaseHeartbeatHandle(store=store, lease=lease, heartbeat_seconds=interval).start()
    _HEARTBEATS[lease.lease_id] = handle
    return handle


def stop_task_lease_auto_heartbeat(lease_id: str, *, release: bool = False, reason: str = "auto_heartbeat_stopped") -> None:
    lease_id = _clean_text(lease_id, 128)
    handle = _HEARTBEATS.pop(lease_id, None)
    if handle is not None:
        handle.close(release=release, reason=reason)


def stop_task_lease_auto_heartbeats_for_store(store: TaskLeaseStore) -> None:
    for lease_id, handle in list(_HEARTBEATS.items()):
        if handle.store is store:
            _HEARTBEATS.pop(lease_id, None)
            handle.close()


def acquire_task_lease_with_heartbeat(
    *,
    store: TaskLeaseStore,
    project: str,
    task_id: str,
    owner_agent: str,
    session_id: str = "",
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    heartbeat_seconds: float | None = None,
    agent_fingerprint: str = "",
    runtime_profile_id: str = "",
    work_token: str = "",
    now: datetime | None = None,
) -> tuple[TaskLeaseClaimResult, TaskLeaseHeartbeatHandle]:
    result = store.claim(
        project=project,
        task_id=task_id,
        owner_agent=owner_agent,
        session_id=session_id,
        agent_fingerprint=agent_fingerprint,
        runtime_profile_id=runtime_profile_id,
        work_token=work_token,
        lease_ttl_seconds=lease_ttl_seconds,
        now=now,
    )
    return result, start_task_lease_auto_heartbeat(
        store=store,
        lease=result.lease,
        heartbeat_seconds=heartbeat_seconds,
    )


_STORE: TaskLeaseStore | None = None


def get_task_lease_store() -> TaskLeaseStore:
    global _STORE
    if _STORE is None:
        _STORE = TaskLeaseStore()
    return _STORE


def close_task_lease_store() -> None:
    global _STORE
    if _STORE is not None:
        stop_task_lease_auto_heartbeats_for_store(_STORE)
        _STORE.close()
        _STORE = None


def verify_work_token_for_mutation(
    *,
    store: TaskLeaseStore,
    lease_id: str,
    work_token: str,
    task_id: str,
    project: str = "mnemoforge",
) -> bool:
    """Verify work token for a mutating operation. Returns True if valid.
    
    Work token recovery is only possible via TTL timeout — there is no lookup
    or recovery path for lost tokens.
    """
    return store.verify_work_token(lease_id=lease_id, work_token=work_token)



def find_continuity_lease_for_mutation(
    *,
    store: TaskLeaseStore,
    project: str,
    task_id: str,
    owner_agent: str,
    session_id: str,
    work_token: str,
) -> TaskLeaseRecord | None:
    """Return the latest non-active lease proving same-owner continuity."""
    return store.latest_continuity_lease(
        project=project,
        task_id=task_id,
        owner_agent=owner_agent,
        session_id=session_id,
        work_token=work_token,
    )

def redact_work_token_from_result(result: dict) -> dict:
    """Remove work_token from any result dict for safe public/log output."""
    if isinstance(result, dict):
        result.pop("work_token", None)
    return result
