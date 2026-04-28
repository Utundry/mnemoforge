from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from uuid import uuid4

from app.models.checkpoint_draft import CheckpointDraftRecord
from app.models.project_task import ProjectTaskChangeCreate
from app.services.memory_scribe_service import draft_task_checkpoint
from app.services.project_task_service import add_task_change
from app.services.stenographer_service import get_stenographer_store


_DB_PATH = Path("qdrant_data") / "checkpoint_drafts.db"
_ALLOWED_PATCH_FIELDS = {
    "summary",
    "blockers",
    "decisions",
    "changed_files",
    "verification",
    "remaining_risk",
    "next_step",
    "stage",
    "status",
    "reason",
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS checkpoint_drafts (
    draft_id                         TEXT NOT NULL,
    version                          INTEGER NOT NULL,
    status                           TEXT NOT NULL,
    project                          TEXT NOT NULL,
    task_id                          TEXT NOT NULL,
    work_id                          TEXT NOT NULL DEFAULT '',
    agent_id                         TEXT NOT NULL DEFAULT 'codex',
    session_id                       TEXT NOT NULL DEFAULT '',
    preview                          TEXT NOT NULL,
    record_task_checkpoint_args_json TEXT NOT NULL,
    validation_report_json           TEXT NOT NULL DEFAULT '{}',
    source_span_ids_json             TEXT NOT NULL DEFAULT '[]',
    metrics_json                     TEXT NOT NULL DEFAULT '{}',
    content_hash                     TEXT NOT NULL,
    created_by                       TEXT NOT NULL DEFAULT 'codex',
    approved_by                      TEXT NOT NULL DEFAULT '',
    rejected_by                      TEXT NOT NULL DEFAULT '',
    rejection_reason                 TEXT NOT NULL DEFAULT '',
    saved_change_id                  TEXT NOT NULL DEFAULT '',
    created_at                       REAL NOT NULL,
    updated_at                       REAL NOT NULL,
    approved_at                      REAL,
    rejected_at                      REAL,
    PRIMARY KEY (draft_id, version)
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_drafts_project_task
    ON checkpoint_drafts(project, task_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_checkpoint_drafts_work
    ON checkpoint_drafts(work_id, updated_at);
"""


class DraftValidationError(ValueError):
    def __init__(self, code: str, message: str, *, draft: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.draft = draft or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), "draft": self.draft}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime | None = None) -> float:
    return (value or _utcnow()).timestamp()


def _dt(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _string_list(value: Any, *, limit: int = 12, item_limit: int = 360) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in items:
        text = _clean_text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _spans_to_raw_notes(spans: list[Any]) -> str:
    label_by_kind = {
        "fact": "Summary",
        "decision": "Decision",
        "verification": "Verification",
        "risk": "Risk",
        "blocker": "Blocker",
        "next_step": "Next",
        "checkpoint_hint": "Summary",
        "handoff_hint": "Next",
        "diagnostic": "Verification",
        "changed_files": "Changed files",
    }
    lines: list[str] = []
    for span in reversed(spans):
        label = label_by_kind.get(str(span.kind), "Summary")
        source = f" [{span.source}]" if getattr(span, "source", "") else ""
        lines.append(f"{label}:{source} {span.content}")
    return "\n".join(lines)


def _preview_from_args(args: dict[str, Any], validation: dict[str, Any], *, source_span_count: int) -> str:
    lines = [
        f"{args.get('stage', 'in_progress')} / {args.get('status', 'active')}: {_clean_text(args.get('summary'), 360)}",
        f"verification={len(args.get('verification') or [])} decisions={len(args.get('decisions') or [])} risks={len(args.get('remaining_risk') or [])} spans={source_span_count}",
    ]
    next_step = _clean_text(args.get("next_step"), 240)
    if next_step:
        lines.append(f"next: {next_step}")
    missing = validation.get("missing") or []
    if missing:
        lines.append(f"needs_review: {', '.join(str(item) for item in missing[:6])}")
    return "\n".join(lines)


def _metrics(args: dict[str, Any], preview: str, source_span_count: int) -> dict[str, Any]:
    full_payload = _json_dumps(args)
    approval_command = {
        "tool": "approve_checkpoint_draft",
        "draft_id": "<draft_id>",
        "version": "<version>",
    }
    full_chars = len(full_payload)
    approval_chars = len(_json_dumps(approval_command))
    return {
        "source_span_count": source_span_count,
        "preview_chars": len(preview),
        "full_payload_chars": full_chars,
        "approval_command_chars": approval_chars,
        "estimated_saved_chars": max(0, full_chars - approval_chars),
        "estimated_saved_tokens": max(0, round((full_chars - approval_chars) / 4)),
    }


def _validate_checkpoint_args(args: dict[str, Any], *, source_span_ids: list[str], quality_gate: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    if not _clean_text(args.get("summary"), 520):
        missing.append("summary")
    if not source_span_ids:
        missing.append("source_evidence")
    for item in quality_gate.get("missing") or []:
        text = _clean_text(item, 80)
        if text and text not in missing:
            missing.append(text)
    if quality_gate.get("blocked_ungrounded"):
        missing.append("grounding_review")
    status = "ready" if not missing and quality_gate.get("status") == "ready" else "needs_review"
    return {
        "status": status,
        "missing": missing,
        "quality_gate": quality_gate,
        "can_approve": status == "ready",
        "mutates_memory": False,
    }


class CheckpointDraftStore:
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

    def _row_to_record(self, row: sqlite3.Row) -> CheckpointDraftRecord:
        return CheckpointDraftRecord(
            draft_id=str(row["draft_id"]),
            version=int(row["version"]),
            status=str(row["status"]),
            project=str(row["project"]),
            task_id=str(row["task_id"]),
            work_id=str(row["work_id"] or ""),
            agent_id=str(row["agent_id"] or "codex"),
            session_id=str(row["session_id"] or ""),
            preview=str(row["preview"]),
            record_task_checkpoint_args=_json_loads(row["record_task_checkpoint_args_json"], {}),
            validation_report=_json_loads(row["validation_report_json"], {}),
            source_span_ids=_json_loads(row["source_span_ids_json"], []),
            metrics=_json_loads(row["metrics_json"], {}),
            content_hash=str(row["content_hash"]),
            created_by=str(row["created_by"] or "codex"),
            approved_by=str(row["approved_by"] or ""),
            rejected_by=str(row["rejected_by"] or ""),
            rejection_reason=str(row["rejection_reason"] or ""),
            saved_change_id=str(row["saved_change_id"] or ""),
            created_at=_dt(row["created_at"]) or _utcnow(),
            updated_at=_dt(row["updated_at"]) or _utcnow(),
            approved_at=_dt(row["approved_at"]),
            rejected_at=_dt(row["rejected_at"]),
        )

    def insert(self, record: CheckpointDraftRecord) -> CheckpointDraftRecord:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO checkpoint_drafts (
                    draft_id, version, status, project, task_id, work_id, agent_id, session_id,
                    preview, record_task_checkpoint_args_json, validation_report_json,
                    source_span_ids_json, metrics_json, content_hash, created_by,
                    approved_by, rejected_by, rejection_reason, saved_change_id,
                    created_at, updated_at, approved_at, rejected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.draft_id,
                    record.version,
                    record.status,
                    record.project,
                    record.task_id,
                    record.work_id,
                    record.agent_id,
                    record.session_id,
                    record.preview,
                    _json_dumps(record.record_task_checkpoint_args),
                    _json_dumps(record.validation_report),
                    _json_dumps(record.source_span_ids),
                    _json_dumps(record.metrics),
                    record.content_hash,
                    record.created_by,
                    record.approved_by,
                    record.rejected_by,
                    record.rejection_reason,
                    record.saved_change_id,
                    _ts(record.created_at),
                    _ts(record.updated_at),
                    _ts(record.approved_at) if record.approved_at else None,
                    _ts(record.rejected_at) if record.rejected_at else None,
                ),
            )
            self._conn.commit()
        return record

    def latest(self, draft_id: str) -> CheckpointDraftRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkpoint_drafts WHERE draft_id=? ORDER BY version DESC LIMIT 1",
                (draft_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get(self, draft_id: str, version: int | None = None) -> CheckpointDraftRecord | None:
        if version is None:
            return self.latest(draft_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkpoint_drafts WHERE draft_id=? AND version=?",
                (draft_id, int(version)),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def update_status(
        self,
        *,
        draft_id: str,
        version: int,
        status: str,
        approved_by: str = "",
        rejected_by: str = "",
        rejection_reason: str = "",
        saved_change_id: str = "",
    ) -> CheckpointDraftRecord:
        now = _utcnow()
        approved_at = now if status == "approved" else None
        rejected_at = now if status == "rejected" else None
        with self._lock:
            self._conn.execute(
                """
                UPDATE checkpoint_drafts
                   SET status=?, approved_by=?, rejected_by=?, rejection_reason=?,
                       saved_change_id=?, updated_at=?, approved_at=?, rejected_at=?
                 WHERE draft_id=? AND version=?
                """,
                (
                    status,
                    approved_by,
                    rejected_by,
                    rejection_reason,
                    saved_change_id,
                    _ts(now),
                    _ts(approved_at) if approved_at else None,
                    _ts(rejected_at) if rejected_at else None,
                    draft_id,
                    version,
                ),
            )
            self._conn.commit()
        record = self.get(draft_id, version)
        if record is None:
            raise DraftValidationError("draft_not_found", "Checkpoint draft not found.")
        return record


_STORE: Optional[CheckpointDraftStore] = None


def get_checkpoint_draft_store() -> CheckpointDraftStore:
    global _STORE
    if _STORE is None:
        _STORE = CheckpointDraftStore()
    return _STORE


def close_checkpoint_draft_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None


async def draft_checkpoint_from_spans(
    payload: dict[str, Any],
    llm_gateway: Any | None = None,
    *,
    store: CheckpointDraftStore | None = None,
) -> CheckpointDraftRecord:
    project = _clean_text(payload.get("project") or "supermemory", 128) or "supermemory"
    task_id = _clean_text(payload.get("task_id"), 256)
    work_id = _clean_text(payload.get("work_id"), 128)
    agent_id = _clean_text(payload.get("agent_id") or "codex", 128) or "codex"
    session_id = _clean_text(payload.get("session_id"), 256)
    spans = get_stenographer_store().list_spans(
        project=project,
        task_id=task_id or None,
        work_id=work_id or None,
        agent_id=agent_id if not work_id else None,
        session_id=session_id or None,
        limit=int(payload.get("limit") or 50),
    )
    if not spans:
        raise DraftValidationError("source_spans_required", "No stenographer spans matched the draft request.")
    raw_notes = _spans_to_raw_notes(spans)
    scribe = await draft_task_checkpoint(
        {
            "project": project,
            "task_id": task_id or spans[0].task_id,
            "task_title": payload.get("task_title") or "",
            "stage": payload.get("stage") or "in_progress",
            "status": payload.get("status") or "active",
            "raw_notes": raw_notes,
            "reason": payload.get("reason") or "draft_checkpoint_from_spans",
            "acted_by": payload.get("created_by") or agent_id,
            "use_llm": bool(payload.get("use_llm", False)),
        },
        llm_gateway=llm_gateway,
    )
    args = dict(scribe["record_task_checkpoint_args"])
    source_span_ids = [span.span_id for span in spans]
    validation = _validate_checkpoint_args(args, source_span_ids=source_span_ids, quality_gate=scribe.get("quality_gate") or {})
    preview = _preview_from_args(args, validation, source_span_count=len(source_span_ids))
    metrics = _metrics(args, preview, len(source_span_ids))
    draft_id = str(uuid4())
    now = _utcnow()
    record = CheckpointDraftRecord(
        draft_id=draft_id,
        version=1,
        status="drafted",
        project=args["project"],
        task_id=args["task_id"],
        work_id=work_id,
        agent_id=agent_id,
        session_id=session_id,
        preview=preview,
        record_task_checkpoint_args=args,
        validation_report=validation,
        source_span_ids=source_span_ids,
        metrics=metrics,
        content_hash=_content_hash(args),
        created_by=_clean_text(payload.get("created_by") or agent_id, 128) or agent_id,
        created_at=now,
        updated_at=now,
    )
    return (store or get_checkpoint_draft_store()).insert(record)


def get_checkpoint_draft(draft_id: str, version: int | None = None, *, store: CheckpointDraftStore | None = None) -> CheckpointDraftRecord:
    record = (store or get_checkpoint_draft_store()).get(draft_id, version)
    if record is None:
        raise DraftValidationError("draft_not_found", "Checkpoint draft not found.")
    return record


def revise_checkpoint_draft(
    draft_id: str,
    patch: dict[str, Any],
    *,
    revised_by: str = "codex",
    store: CheckpointDraftStore | None = None,
) -> CheckpointDraftRecord:
    draft_store = store or get_checkpoint_draft_store()
    current = draft_store.latest(draft_id)
    if current is None:
        raise DraftValidationError("draft_not_found", "Checkpoint draft not found.")
    if current.status in {"approved", "rejected", "expired"}:
        raise DraftValidationError("draft_terminal", "Cannot revise a terminal checkpoint draft.", draft=current.model_dump(mode="json"))
    disallowed = sorted(set(patch) - _ALLOWED_PATCH_FIELDS)
    if disallowed:
        raise DraftValidationError("invalid_patch_fields", f"Patch contains unsupported fields: {', '.join(disallowed)}")
    args = dict(current.record_task_checkpoint_args)
    for key, value in patch.items():
        if key in {"blockers", "decisions", "changed_files", "verification", "remaining_risk"}:
            args[key] = _string_list(value)
        elif key in {"summary", "next_step", "reason"}:
            args[key] = _clean_text(value, 700 if key == "summary" else 360)
        elif key == "stage":
            args[key] = _clean_text(value, 32) or args.get(key)
        elif key == "status":
            args[key] = _clean_text(value, 32) or args.get(key)
    validation = _validate_checkpoint_args(args, source_span_ids=current.source_span_ids, quality_gate=current.validation_report.get("quality_gate") or {"status": "ready"})
    preview = _preview_from_args(args, validation, source_span_count=len(current.source_span_ids))
    metrics = _metrics(args, preview, len(current.source_span_ids))
    now = _utcnow()
    revised = CheckpointDraftRecord(
        **{
            **current.model_dump(),
            "version": current.version + 1,
            "status": "revised",
            "preview": preview,
            "record_task_checkpoint_args": args,
            "validation_report": validation,
            "metrics": metrics,
            "content_hash": _content_hash(args),
            "created_by": _clean_text(revised_by, 128) or current.created_by,
            "updated_at": now,
            "approved_by": "",
            "rejected_by": "",
            "rejection_reason": "",
            "saved_change_id": "",
            "approved_at": None,
            "rejected_at": None,
        }
    )
    return draft_store.insert(revised)


async def approve_checkpoint_draft(
    draft_id: str,
    version: int,
    *,
    approved_by: str = "codex",
    qdrant: Any | None = None,
    ollama: Any | None = None,
    save_checkpoint: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    store: CheckpointDraftStore | None = None,
) -> CheckpointDraftRecord:
    draft_store = store or get_checkpoint_draft_store()
    current = draft_store.latest(draft_id)
    if current is None:
        raise DraftValidationError("draft_not_found", "Checkpoint draft not found.")
    if current.version != int(version):
        raise DraftValidationError("stale_draft_version", "Approve requires the latest checked draft version.", draft=current.model_dump(mode="json"))
    if current.status not in {"drafted", "revised"}:
        raise DraftValidationError("draft_not_approvable", "Checkpoint draft is not approvable.", draft=current.model_dump(mode="json"))
    if not current.validation_report.get("can_approve"):
        raise DraftValidationError("draft_validation_failed", "Checkpoint draft validation failed; revise before approval.", draft=current.model_dump(mode="json"))

    args = dict(current.record_task_checkpoint_args)
    if save_checkpoint is not None:
        saved = await save_checkpoint(args)
    else:
        if qdrant is None or ollama is None:
            raise DraftValidationError("save_context_required", "qdrant and ollama are required to approve without a save callback.")
        from app.services.mcp_tool_contracts import build_report_task_checkpoint_payload

        payload = build_report_task_checkpoint_payload(args)
        change = await add_task_change(
            qdrant,
            ollama,
            task_id=str(args["task_id"]),
            body=ProjectTaskChangeCreate(**payload),
        )
        saved = change.model_dump(mode="json")
    saved_change_id = str(saved.get("id") or saved.get("change_id") or "")
    return draft_store.update_status(
        draft_id=draft_id,
        version=current.version,
        status="approved",
        approved_by=_clean_text(approved_by, 128) or "codex",
        saved_change_id=saved_change_id,
    )


def reject_checkpoint_draft(
    draft_id: str,
    version: int,
    *,
    rejected_by: str = "codex",
    reason: str = "",
    store: CheckpointDraftStore | None = None,
) -> CheckpointDraftRecord:
    draft_store = store or get_checkpoint_draft_store()
    current = draft_store.latest(draft_id)
    if current is None:
        raise DraftValidationError("draft_not_found", "Checkpoint draft not found.")
    if current.version != int(version):
        raise DraftValidationError("stale_draft_version", "Reject requires the latest draft version.", draft=current.model_dump(mode="json"))
    if current.status in {"approved", "rejected", "expired"}:
        raise DraftValidationError("draft_terminal", "Checkpoint draft is already terminal.", draft=current.model_dump(mode="json"))
    return draft_store.update_status(
        draft_id=draft_id,
        version=current.version,
        status="rejected",
        rejected_by=_clean_text(rejected_by, 128) or "codex",
        rejection_reason=_clean_text(reason, 500),
    )
