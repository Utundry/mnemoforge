from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable, Optional
from uuid import uuid4

from app.models.stenographer import (
    STENOGRAPHER_KIND_PATTERN,
    WORK_SESSION_TERMINAL_STATUSES,
    StenographerSpanRecord,
    WorkSessionRecord,
    WorkSessionState,
)


_DB_PATH = Path("qdrant_data") / "stenographer.db"
_VALID_SPAN_KINDS = set(re.findall(r"\w+", STENOGRAPHER_KIND_PATTERN.split("^(", 1)[1].split(")$", 1)[0]))
_VALID_TERMINAL_STATUSES = WORK_SESSION_TERMINAL_STATUSES
_COMPLETED_CLOSEOUT_KINDS = ("verification", "changed_files", "next_step")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS work_sessions (
    work_id            TEXT PRIMARY KEY,
    project            TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    role               TEXT NOT NULL DEFAULT 'worker',
    status             TEXT NOT NULL,
    parent_work_id     TEXT NOT NULL DEFAULT '',
    parent_task_id     TEXT NOT NULL DEFAULT '',
    spawn_reason       TEXT NOT NULL DEFAULT '',
    return_condition   TEXT NOT NULL DEFAULT '',
    scope_json         TEXT NOT NULL DEFAULT '[]',
    summary            TEXT NOT NULL DEFAULT '',
    result             TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    ended_at           REAL
);
CREATE INDEX IF NOT EXISTS idx_work_sessions_agent_session_status
    ON work_sessions(agent_id, session_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_work_sessions_project_task
    ON work_sessions(project, task_id, updated_at);

CREATE TABLE IF NOT EXISTS stenographer_spans (
    span_id                 TEXT PRIMARY KEY,
    project                 TEXT NOT NULL,
    task_id                 TEXT NOT NULL,
    work_id                 TEXT NOT NULL,
    agent_id                TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    source                  TEXT NOT NULL DEFAULT '',
    content                 TEXT NOT NULL,
    content_hash            TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'active',
    redaction_report_json   TEXT NOT NULL DEFAULT '[]',
    excluded_from_learning  INTEGER NOT NULL DEFAULT 1,
    created_at              REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stenographer_span_dedup
    ON stenographer_spans(work_id, kind, source, content_hash);
CREATE INDEX IF NOT EXISTS idx_stenographer_spans_work
    ON stenographer_spans(work_id, created_at);
"""


class ProtocolViolation(ValueError):
    def __init__(self, code: str, message: str, *, required_next_tool: str = "", state: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.required_next_tool = required_next_tool
        self.state = state or {}

    def to_dict(self) -> dict:
        return {
            "error": self.code,
            "message": str(self),
            "required_next_tool": self.required_next_tool,
            "current_state": self.state,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime | None = None) -> float:
    return (value or _utcnow()).timestamp()


def _dt(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _json_list(values: Iterable[str] | None) -> str:
    return json.dumps([str(item).strip() for item in (values or []) if str(item).strip()])


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _clean_text(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def _redact_content(content: str) -> tuple[str, list[str]]:
    redactions: list[str] = []
    text = str(content or "")
    patterns = [
        ("api_key", re.compile(r"(?i)\b(api[_-]?key|x-api-key)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{8,})['\"]?")),
        ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
        ("env_secret", re.compile(r"(?im)^\s*[A-Z0-9_]*(SECRET|TOKEN|PASSWORD|KEY)\s*=\s*.+$")),
    ]
    for label, pattern in patterns:
        updated = pattern.sub(f"[REDACTED:{label}]", text)
        if updated != text:
            redactions.append(label)
            text = updated
    return text, sorted(set(redactions))


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class StenographerStore:
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

    def _row_to_work(self, row: sqlite3.Row) -> WorkSessionRecord:
        return WorkSessionRecord(
            work_id=str(row["work_id"]),
            project=str(row["project"]),
            task_id=str(row["task_id"]),
            agent_id=str(row["agent_id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
            parent_work_id=str(row["parent_work_id"] or ""),
            parent_task_id=str(row["parent_task_id"] or ""),
            spawn_reason=str(row["spawn_reason"] or ""),
            return_condition=str(row["return_condition"] or ""),
            scope=_parse_json_list(row["scope_json"]),
            summary=str(row["summary"] or ""),
            result=str(row["result"] or ""),
            created_at=_dt(row["created_at"]) or _utcnow(),
            updated_at=_dt(row["updated_at"]) or _utcnow(),
            ended_at=_dt(row["ended_at"]),
        )

    def _row_to_span(self, row: sqlite3.Row) -> StenographerSpanRecord:
        return StenographerSpanRecord(
            span_id=str(row["span_id"]),
            project=str(row["project"]),
            task_id=str(row["task_id"]),
            work_id=str(row["work_id"]),
            agent_id=str(row["agent_id"]),
            session_id=str(row["session_id"]),
            kind=str(row["kind"]),
            source=str(row["source"] or ""),
            content=str(row["content"]),
            content_hash=str(row["content_hash"]),
            status=str(row["status"]),
            redaction_report=_parse_json_list(row["redaction_report_json"]),
            excluded_from_learning=bool(row["excluded_from_learning"]),
            created_at=_dt(row["created_at"]) or _utcnow(),
        )

    def _active_work(self, *, agent_id: str, session_id: str) -> WorkSessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM work_sessions
                 WHERE agent_id = ? AND session_id = ? AND status = 'active'
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (agent_id, session_id),
            ).fetchone()
        return self._row_to_work(row) if row else None

    def _work(self, work_id: str) -> WorkSessionRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM work_sessions WHERE work_id = ?", (work_id,)).fetchone()
        return self._row_to_work(row) if row else None

    def _closeout_review(self, work: WorkSessionRecord | None) -> dict[str, object]:
        if work is None:
            return {"required": False, "ready": False, "missing": [], "evidence": {}}
        evidence: dict[str, list[str]] = {kind: [] for kind in _COMPLETED_CLOSEOUT_KINDS}
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT span_id, kind FROM stenographer_spans
                 WHERE work_id = ? AND status = 'active'
                   AND kind IN ('verification', 'changed_files', 'next_step')
                 ORDER BY created_at ASC
                """,
                (work.work_id,),
            ).fetchall()
        for row in rows:
            kind = str(row["kind"])
            if kind in evidence:
                evidence[kind].append(str(row["span_id"]))
        missing = [kind for kind, span_ids in evidence.items() if not span_ids]
        return {
            "required": True,
            "ready": not missing,
            "missing": missing,
            "evidence": evidence,
        }

    def get_state(self, *, agent_id: str, session_id: str) -> WorkSessionState:
        active = self._active_work(agent_id=agent_id, session_id=session_id)
        with self._lock:
            parked_rows = self._conn.execute(
                """
                SELECT * FROM work_sessions
                 WHERE agent_id = ? AND session_id = ? AND status = 'parked'
                 ORDER BY updated_at DESC
                """,
                (agent_id, session_id),
            ).fetchall()
        parked = [self._row_to_work(row) for row in parked_rows]
        state = "active_work" if active else ("parked_parent" if parked else "no_active_work")
        next_tools = {
            "no_active_work": ["start_work_session", "continue_task", "list_open_tasks", "get_work_session_state"],
            "parked_parent": ["start_work_session", "resume_work_session", "get_work_session_state"],
            "active_work": [
                "record_stenographer_span",
                "park_work_session",
                "end_work_session",
                "list_stenographer_spans",
                "draft_checkpoint_from_spans",
                "get_work_session_state",
            ],
        }[state]
        closeout = self._closeout_review(active) if active else {"required": False, "ready": False, "missing": [], "evidence": {}}
        protocol_violations = []
        if active and closeout["missing"]:
            protocol_violations.append("closeout_incomplete")
        return WorkSessionState(
            project=active.project if active else (parked[0].project if parked else ""),
            task_id=active.task_id if active else (parked[0].task_id if parked else ""),
            agent_id=agent_id,
            session_id=session_id,
            state=state,
            active_work=active,
            parked_stack=parked,
            next_valid_tools=next_tools,
            protocol_violations=protocol_violations,
            closeout_required=bool(closeout["required"]),
            closeout_ready=bool(closeout["ready"]),
            closeout_missing=list(closeout["missing"]),  # type: ignore[arg-type]
            closeout_evidence=closeout["evidence"],  # type: ignore[arg-type]
        )

    def start_work_session(
        self,
        *,
        project: str,
        task_id: str,
        agent_id: str,
        session_id: str,
        role: str = "worker",
        work_id: str | None = None,
        parent_work_id: str = "",
        parent_task_id: str = "",
        spawn_reason: str = "",
        return_condition: str = "",
        scope: Iterable[str] | None = None,
        summary: str = "",
    ) -> WorkSessionRecord:
        project = _clean_text(project or "supermemory", 128) or "supermemory"
        task_id = _clean_text(task_id, 256)
        agent_id = _clean_text(agent_id or "codex", 128) or "codex"
        session_id = _clean_text(session_id or agent_id, 256) or agent_id
        if not task_id:
            raise ProtocolViolation("task_id_required", "task_id is required to start a work session.", required_next_tool="start_work_session")
        active = self._active_work(agent_id=agent_id, session_id=session_id)
        if active:
            raise ProtocolViolation(
                "active_work_exists",
                "Cannot start a second active work session; park or end the current work first.",
                required_next_tool="park_work_session",
                state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
            )
        if parent_work_id:
            parent = self._work(parent_work_id)
            if not parent or parent.status != "parked":
                raise ProtocolViolation(
                    "parent_not_parked",
                    "Child work requires an existing parked parent work session.",
                    required_next_tool="park_work_session",
                    state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
                )
            parent_task_id = parent_task_id or parent.task_id
        now = _ts()
        resolved_work_id = _clean_text(work_id, 128) or str(uuid4())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO work_sessions (
                    work_id, project, task_id, agent_id, session_id, role, status,
                    parent_work_id, parent_task_id, spawn_reason, return_condition,
                    scope_json, summary, result, created_at, updated_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, '', ?, ?, NULL)
                """,
                (
                    resolved_work_id,
                    project,
                    task_id,
                    agent_id,
                    session_id,
                    _clean_text(role or "worker", 64) or "worker",
                    _clean_text(parent_work_id, 128),
                    _clean_text(parent_task_id, 256),
                    _clean_text(spawn_reason, 1000),
                    _clean_text(return_condition, 1000),
                    _json_list(scope),
                    _clean_text(summary, 1200),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self._work(resolved_work_id)  # type: ignore[return-value]

    def park_work_session(
        self,
        *,
        work_id: str,
        agent_id: str,
        session_id: str,
        reason: str,
        child_task_id: str = "",
        child_work_id: str = "",
    ) -> WorkSessionRecord:
        current = self._active_work(agent_id=agent_id, session_id=session_id)
        if not current or current.work_id != work_id:
            raise ProtocolViolation(
                "active_work_mismatch",
                "Only the current active work session can be parked.",
                required_next_tool="get_work_session_state",
                state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
            )
        summary = _clean_text(reason, 1000)
        if child_task_id or child_work_id:
            summary = (summary + f" child_task_id={_clean_text(child_task_id, 256)} child_work_id={_clean_text(child_work_id, 128)}").strip()
        now = _ts()
        with self._lock:
            self._conn.execute(
                "UPDATE work_sessions SET status='parked', summary=?, updated_at=? WHERE work_id=?",
                (summary, now, work_id),
            )
            self._conn.commit()
        return self._work(work_id)  # type: ignore[return-value]

    def resume_work_session(
        self,
        *,
        work_id: str,
        agent_id: str,
        session_id: str,
        child_work_id: str = "",
        result: str = "",
    ) -> WorkSessionRecord:
        active = self._active_work(agent_id=agent_id, session_id=session_id)
        if active:
            raise ProtocolViolation(
                "active_work_exists",
                "Cannot resume a parked parent while another work session is active.",
                required_next_tool="end_work_session",
                state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
            )
        parent = self._work(work_id)
        if not parent or parent.status != "parked":
            raise ProtocolViolation("parked_work_not_found", "No matching parked work session found.", required_next_tool="get_work_session_state")
        if child_work_id:
            child = self._work(child_work_id)
            if not child or child.status not in _VALID_TERMINAL_STATUSES:
                raise ProtocolViolation(
                    "child_not_terminal",
                    "Parent resume requires the child work session to be terminal.",
                    required_next_tool="end_work_session",
                    state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
                )
        now = _ts()
        with self._lock:
            self._conn.execute(
                "UPDATE work_sessions SET status='active', result=?, updated_at=? WHERE work_id=?",
                (_clean_text(result, 1200), now, work_id),
            )
            self._conn.commit()
        return self._work(work_id)  # type: ignore[return-value]

    def end_work_session(
        self,
        *,
        work_id: str,
        task_id: str,
        agent_id: str,
        session_id: str,
        status: str,
        result: str = "",
    ) -> WorkSessionRecord:
        status = _clean_text(status, 32)
        if status not in _VALID_TERMINAL_STATUSES:
            raise ProtocolViolation("invalid_terminal_status", "End status must be completed, blocked, failed, interrupted, or cancelled.")
        current = self._active_work(agent_id=agent_id, session_id=session_id)
        if not current or current.work_id != work_id or current.task_id != task_id:
            raise ProtocolViolation(
                "active_work_mismatch",
                "SM_WORK_END must match the current active work_id and task_id.",
                required_next_tool="get_work_session_state",
                state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
            )
        if status == "completed":
            closeout = self._closeout_review(current)
            if not closeout["ready"]:
                raise ProtocolViolation(
                    "closeout_required",
                    "Completed work requires explicit closeout evidence: verification, changed_files, and next_step spans.",
                    required_next_tool="record_stenographer_span",
                    state={
                        **self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
                        "closeout_missing": closeout["missing"],
                    },
                )
        now = _ts()
        with self._lock:
            self._conn.execute(
                "UPDATE work_sessions SET status=?, result=?, updated_at=?, ended_at=? WHERE work_id=?",
                (status, _clean_text(result, 2000), now, now, work_id),
            )
            self._conn.commit()
        return self._work(work_id)  # type: ignore[return-value]

    def record_span(
        self,
        *,
        project: str,
        task_id: str,
        agent_id: str,
        session_id: str,
        kind: str,
        content: str,
        source: str = "",
        work_id: str = "",
    ) -> StenographerSpanRecord:
        kind = _clean_text(kind, 64)
        if kind not in _VALID_SPAN_KINDS:
            raise ProtocolViolation("invalid_span_kind", "Invalid stenographer span kind.", required_next_tool="record_stenographer_span")
        active = self._active_work(agent_id=agent_id, session_id=session_id)
        if not active:
            raise ProtocolViolation(
                "work_session_required",
                "No active work session; start_work_session is required before recording stenographer spans.",
                required_next_tool="start_work_session",
                state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
            )
        if work_id and work_id != active.work_id:
            raise ProtocolViolation(
                "active_work_mismatch",
                "Span work_id must match the active work session.",
                required_next_tool="get_work_session_state",
                state=self.get_state(agent_id=agent_id, session_id=session_id).model_dump(mode="json"),
            )
        clean_content, redactions = _redact_content(str(content or "").strip()[:8192])
        if not clean_content:
            raise ProtocolViolation("span_content_required", "Span content is required.", required_next_tool="record_stenographer_span")
        content_hash = _hash_content(clean_content)
        span_id = str(uuid4())
        now = _ts()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT * FROM stenographer_spans
                 WHERE work_id=? AND kind=? AND source=? AND content_hash=?
                 LIMIT 1
                """,
                (active.work_id, kind, _clean_text(source, 128), content_hash),
            ).fetchone()
            if existing:
                return self._row_to_span(existing)
            self._conn.execute(
                """
                INSERT INTO stenographer_spans (
                    span_id, project, task_id, work_id, agent_id, session_id,
                    kind, source, content, content_hash, status, redaction_report_json,
                    excluded_from_learning, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 1, ?)
                """,
                (
                    span_id,
                    project or active.project,
                    task_id or active.task_id,
                    active.work_id,
                    agent_id,
                    session_id,
                    kind,
                    _clean_text(source, 128),
                    clean_content,
                    content_hash,
                    json.dumps(redactions),
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM stenographer_spans WHERE span_id=?", (span_id,)).fetchone()
        return self._row_to_span(row)

    def list_spans(
        self,
        *,
        project: str | None = None,
        task_id: str | None = None,
        work_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[StenographerSpanRecord]:
        clauses: list[str] = []
        params: list[object] = []
        for field, value in (
            ("project", project),
            ("task_id", task_id),
            ("work_id", work_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
        ):
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 100)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM stenographer_spans {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_span(row) for row in rows]

    def list_spans_after(
        self,
        *,
        since_ts: float,
        kinds: Iterable[str] | None = None,
        project: str | None = None,
        limit: int = 500,
    ) -> list[StenographerSpanRecord]:
        clauses = ["created_at > ?"]
        params: list[object] = [float(since_ts)]
        clean_kinds = [_clean_text(kind, 64) for kind in (kinds or []) if _clean_text(kind, 64)]
        if clean_kinds:
            placeholders = ",".join("?" for _ in clean_kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(clean_kinds)
        if project:
            clauses.append("project = ?")
            params.append(project)
        params.append(max(1, min(int(limit), 2000)))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM stenographer_spans
                 WHERE {' AND '.join(clauses)}
                 ORDER BY created_at ASC
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_span(row) for row in rows]


_STORE: Optional[StenographerStore] = None


def get_stenographer_store() -> StenographerStore:
    global _STORE
    if _STORE is None:
        _STORE = StenographerStore()
    return _STORE


def close_stenographer_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None
