from __future__ import annotations

import json
import re
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Optional
from uuid import uuid4

from app.models.rule_lifecycle import (
    RULE_MARKER_KINDS,
    RuleCandidateProjectionReport,
    RuleCandidateRecord,
    RuleCandidateReviewActionRequest,
    RuleCandidateReviewActionResponse,
    RuleCandidateReviewItem,
    RuleCandidateReviewPacket,
    RuleCandidateReviewRequest,
    RuleCandidateSimilarityMatch,
    RuleCandidatePromoteRequest,
    RuleCandidatePromoteResponse,
    RuleCandidateReviseLawRequest,
    RuleCandidateReviseLawResponse,
    RuleCandidateTrialExpireRequest,
    RuleCandidateTrialExpireResponse,
)
from app.models.stenographer import StenographerSpanRecord
from app.models.law import ProjectLawConfirmRequest, ProjectLawCreate, ProjectLawUpdate
from app.services.law_service import (
    confirm_project_law,
    create_project_law,
    get_project_law,
    list_project_laws,
    update_project_law,
    update_project_law_status,
)
from app.services.stenographer_service import get_stenographer_store
from app.services.system_data_root import data_path


_DB_PATH = data_path("rule_lifecycle.db")
_PROJECTOR_KEY = "stenographer_rule_candidates"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS rule_candidates (
    candidate_id        TEXT PRIMARY KEY,
    project             TEXT NOT NULL,
    scope               TEXT NOT NULL,
    topic_path          TEXT NOT NULL DEFAULT '',
    marker_kind         TEXT NOT NULL,
    statement           TEXT NOT NULL,
    rationale           TEXT NOT NULL DEFAULT '',
    evidence_refs_json  TEXT NOT NULL DEFAULT '[]',
    source_task_id      TEXT NOT NULL DEFAULT '',
    source_session_id   TEXT NOT NULL DEFAULT '',
    source_span_id      TEXT NOT NULL UNIQUE,
    source_work_id      TEXT NOT NULL DEFAULT '',
    confidence          REAL NOT NULL DEFAULT 0.5,
    promotion_hint      TEXT NOT NULL DEFAULT '',
    related_rule_hint   TEXT,
    status              TEXT NOT NULL DEFAULT 'candidate',
    last_review_action  TEXT NOT NULL DEFAULT '',
    last_review_reason  TEXT NOT NULL DEFAULT '',
    last_review_acted_by TEXT NOT NULL DEFAULT '',
    last_review_source  TEXT NOT NULL DEFAULT '',
    last_review_at      REAL,
    promoted_law_id     TEXT NOT NULL DEFAULT '',
    promoted_at         REAL,
    revised_law_id      TEXT NOT NULL DEFAULT '',
    revised_at          REAL,
    trial_started_at    REAL,
    trial_review_after  REAL,
    trial_expires_at    REAL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_candidates_project_status_topic
    ON rule_candidates(project, status, topic_path, updated_at);
CREATE INDEX IF NOT EXISTS idx_rule_candidates_source_task
    ON rule_candidates(project, source_task_id, updated_at);

CREATE TABLE IF NOT EXISTS rule_projector_state (
    projector_key             TEXT PRIMARY KEY,
    last_processed_timestamp  REAL NOT NULL DEFAULT 0
);
"""


class RuleMarkerValidationError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime | None = None) -> float:
    return (value or _utcnow()).timestamp()


def _dt(value: float | None) -> datetime:
    return datetime.fromtimestamp(float(value or 0), tz=timezone.utc)


def _days_from_now(days: int | float) -> float:
    return _ts() + max(0.0, float(days)) * 86400.0


def _clean_text(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def _json_list(values: Iterable[str] | None) -> str:
    return json.dumps([str(item).strip() for item in (values or []) if str(item).strip()])


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []
    except Exception:
        return []


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[;\n,]+", text) if item.strip()]


def _parse_marker_content(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        raise RuleMarkerValidationError("rule marker content is required")
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleMarkerValidationError(f"invalid marker JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise RuleMarkerValidationError("marker JSON must be an object")
        return parsed

    parsed: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()
    if not parsed:
        raise RuleMarkerValidationError("marker content must be JSON or key: value lines")
    return parsed


def _scope_for_marker(kind: str) -> str:
    return "canonical_candidate" if kind == "rule_canonical_candidate" else "project"


def build_rule_candidate_from_span(span: StenographerSpanRecord) -> dict[str, Any]:
    if span.kind not in RULE_MARKER_KINDS:
        raise RuleMarkerValidationError(f"span kind {span.kind!r} is not a rule marker")
    payload = _parse_marker_content(span.content)
    statement = _clean_text(payload.get("statement"), 1200)
    rationale = _clean_text(payload.get("rationale"), 2000)
    if not statement:
        raise RuleMarkerValidationError("statement is required")
    if not rationale:
        raise RuleMarkerValidationError("rationale is required")
    confidence_raw = payload.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "project": _clean_text(payload.get("project") or span.project, 128) or span.project,
        "scope": _scope_for_marker(span.kind),
        "topic_path": _clean_text(payload.get("topic_path"), 256),
        "marker_kind": span.kind,
        "statement": statement,
        "rationale": rationale,
        "evidence_refs": _as_list(payload.get("evidence_refs")) or [f"stenographer_span:{span.span_id}"],
        "source_task_id": span.task_id,
        "source_session_id": span.session_id,
        "source_span_id": span.span_id,
        "source_work_id": span.work_id,
        "confidence": confidence,
        "promotion_hint": _clean_text(payload.get("promotion_hint"), 1000),
        "related_rule_hint": _clean_text(payload.get("related_rule_hint"), 512) or None,
        "status": "candidate",
    }


def build_rule_candidate_from_direct_request(body) -> dict[str, Any]:
    statement = _clean_text(body.statement, 1200)
    if not statement:
        raise RuleMarkerValidationError("statement is required")
    rationale = _clean_text(body.rationale, 2000)
    evidence_refs = _as_list(body.evidence_refs)
    source_span_id = _clean_text(body.source_span_id, 256)
    if not source_span_id:
        seed = "|".join(
            [
                _clean_text(body.project, 128),
                _clean_text(body.scope, 64),
                _clean_text(body.status, 64),
                statement,
                rationale,
            ]
        )
        source_span_id = "direct-rule:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return {
        "project": _clean_text(body.project, 128),
        "scope": _clean_text(body.scope, 64) or "project",
        "topic_path": _clean_text(body.topic_path, 256),
        "marker_kind": "rule_canonical_candidate" if body.scope == "canonical_candidate" else "rule_project_candidate",
        "statement": statement,
        "rationale": rationale or "Direct rule proposal from project_rules.",
        "evidence_refs": evidence_refs or [f"project_rules:{_clean_text(body.source, 128) or 'direct'}"],
        "source_task_id": _clean_text(body.source_task_id, 256),
        "source_session_id": _clean_text(body.source_session_id, 256),
        "source_span_id": source_span_id,
        "source_work_id": _clean_text(body.source_work_id, 256),
        "confidence": max(0.0, min(1.0, float(body.confidence or 0.75))),
        "promotion_hint": _clean_text(body.promotion_hint, 1000),
        "related_rule_hint": _clean_text(body.related_rule_hint, 512) or None,
        "status": _clean_text(body.status, 64) or "trial",
        "trial_started_at": _ts() if body.status == "trial" else None,
        "trial_review_after": _days_from_now(body.review_after_days) if body.status == "trial" else None,
        "trial_expires_at": _days_from_now(body.trial_days) if body.status == "trial" else None,
    }


class RuleLifecycleStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._ensure_columns()
            self._conn.commit()

    def _ensure_columns(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(rule_candidates)").fetchall()
        existing = {str(row["name"]) for row in rows}
        for name, definition in (
            ("last_review_action", "TEXT NOT NULL DEFAULT ''"),
            ("last_review_reason", "TEXT NOT NULL DEFAULT ''"),
            ("last_review_acted_by", "TEXT NOT NULL DEFAULT ''"),
            ("last_review_source", "TEXT NOT NULL DEFAULT ''"),
            ("last_review_at", "REAL"),
            ("promoted_law_id", "TEXT NOT NULL DEFAULT ''"),
            ("promoted_at", "REAL"),
            ("revised_law_id", "TEXT NOT NULL DEFAULT ''"),
            ("revised_at", "REAL"),
            ("trial_started_at", "REAL"),
            ("trial_review_after", "REAL"),
            ("trial_expires_at", "REAL"),
        ):
            if name not in existing:
                self._conn.execute(f"ALTER TABLE rule_candidates ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row_to_candidate(self, row: sqlite3.Row) -> RuleCandidateRecord:
        return RuleCandidateRecord(
            candidate_id=str(row["candidate_id"]),
            project=str(row["project"]),
            scope=str(row["scope"]),
            topic_path=str(row["topic_path"] or ""),
            marker_kind=str(row["marker_kind"]),
            statement=str(row["statement"]),
            rationale=str(row["rationale"] or ""),
            evidence_refs=_parse_json_list(row["evidence_refs_json"]),
            source_task_id=str(row["source_task_id"] or ""),
            source_session_id=str(row["source_session_id"] or ""),
            source_span_id=str(row["source_span_id"]),
            source_work_id=str(row["source_work_id"] or ""),
            confidence=float(row["confidence"] or 0.5),
            promotion_hint=str(row["promotion_hint"] or ""),
            related_rule_hint=row["related_rule_hint"],
            status=str(row["status"] or "candidate"),
            last_review_action=str(row["last_review_action"] or ""),
            last_review_reason=str(row["last_review_reason"] or ""),
            last_review_acted_by=str(row["last_review_acted_by"] or ""),
            last_review_source=str(row["last_review_source"] or ""),
            last_review_at=_dt(row["last_review_at"]) if row["last_review_at"] is not None else None,
            promoted_law_id=str(row["promoted_law_id"] or ""),
            promoted_at=_dt(row["promoted_at"]) if row["promoted_at"] is not None else None,
            revised_law_id=str(row["revised_law_id"] or ""),
            revised_at=_dt(row["revised_at"]) if row["revised_at"] is not None else None,
            trial_started_at=_dt(row["trial_started_at"]) if row["trial_started_at"] is not None else None,
            trial_review_after=_dt(row["trial_review_after"]) if row["trial_review_after"] is not None else None,
            trial_expires_at=_dt(row["trial_expires_at"]) if row["trial_expires_at"] is not None else None,
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def get_last_processed_timestamp(self, projector_key: str = _PROJECTOR_KEY) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_processed_timestamp FROM rule_projector_state WHERE projector_key = ?",
                (projector_key,),
            ).fetchone()
        return float(row["last_processed_timestamp"]) if row else 0.0

    def set_last_processed_timestamp(self, value: float, projector_key: str = _PROJECTOR_KEY) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rule_projector_state (projector_key, last_processed_timestamp)
                VALUES (?, ?)
                ON CONFLICT(projector_key) DO UPDATE
                SET last_processed_timestamp = excluded.last_processed_timestamp
                """,
                (projector_key, float(value)),
            )
            self._conn.commit()

    def create_candidate(self, payload: dict[str, Any]) -> RuleCandidateRecord:
        now = _ts()
        candidate_id = str(uuid4())
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE source_span_id = ?",
                (payload["source_span_id"],),
            ).fetchone()
            if existing:
                return self._row_to_candidate(existing)
            self._conn.execute(
                """
                INSERT INTO rule_candidates (
                    candidate_id, project, scope, topic_path, marker_kind,
                    statement, rationale, evidence_refs_json, source_task_id,
                    source_session_id, source_span_id, source_work_id, confidence,
                    promotion_hint, related_rule_hint, status,
                    trial_started_at, trial_review_after, trial_expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    payload["project"],
                    payload["scope"],
                    payload["topic_path"],
                    payload["marker_kind"],
                    payload["statement"],
                    payload["rationale"],
                    _json_list(payload["evidence_refs"]),
                    payload["source_task_id"],
                    payload["source_session_id"],
                    payload["source_span_id"],
                    payload["source_work_id"],
                    payload["confidence"],
                    payload["promotion_hint"],
                    payload["related_rule_hint"],
                    payload["status"],
                    payload.get("trial_started_at"),
                    payload.get("trial_review_after"),
                    payload.get("trial_expires_at"),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM rule_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        return self._row_to_candidate(row)

    def list_candidates(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        source_task_id: str | None = None,
        review_due: bool = False,
        limit: int = 100,
    ) -> list[RuleCandidateRecord]:
        clauses: list[str] = []
        params: list[object] = []
        for field, value in (
            ("project", project),
            ("status", status),
            ("source_task_id", source_task_id),
        ):
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        if review_due:
            clauses.append("(trial_review_after IS NOT NULL AND trial_review_after <= ?)")
            params.append(_ts())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM rule_candidates {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_candidate(row) for row in rows]

    def expire_trial_candidates(
        self,
        *,
        project: str | None = None,
        limit: int = 100,
        reason: str,
        acted_by: str,
        source: str,
    ) -> RuleCandidateTrialExpireResponse:
        now = _ts()
        clauses = ["status = 'trial'", "trial_expires_at IS NOT NULL", "trial_expires_at <= ?"]
        params: list[object] = [now]
        if project:
            clauses.append("project = ?")
            params.append(project)
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM rule_candidates WHERE {' AND '.join(clauses)} ORDER BY trial_expires_at ASC LIMIT ?",
                params,
            ).fetchall()
            ids = [str(row["candidate_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"""
                    UPDATE rule_candidates
                    SET status = 'suppressed',
                        last_review_action = 'expire_trial',
                        last_review_reason = ?,
                        last_review_acted_by = ?,
                        last_review_source = ?,
                        last_review_at = ?,
                        updated_at = ?
                    WHERE candidate_id IN ({placeholders})
                    """,
                    (
                        _clean_text(reason, 1000),
                        _clean_text(acted_by, 256) or "system",
                        _clean_text(source, 128) or "rule_candidate_trial_expiry",
                        now,
                        now,
                        *ids,
                    ),
                )
                self._conn.commit()
            updated = []
            if ids:
                placeholders = ",".join("?" for _ in ids)
                updated = self._conn.execute(
                    f"SELECT * FROM rule_candidates WHERE candidate_id IN ({placeholders}) ORDER BY updated_at DESC",
                    ids,
                ).fetchall()
        candidates = [self._row_to_candidate(row) for row in updated]
        return RuleCandidateTrialExpireResponse(expired_count=len(candidates), candidates=candidates)

    def review_candidate(
        self,
        candidate_id: str,
        *,
        action: str,
        reason: str,
        acted_by: str,
        source: str,
    ) -> RuleCandidateReviewActionResponse:
        next_status_by_action = {
            "reject": "rejected",
            "suppress": "suppressed",
            "needs_clarification": "needs_clarification",
            "reopen": "candidate",
        }
        if action not in next_status_by_action:
            raise ValueError(f"Unsupported rule candidate review action: {action}")
        now = _ts()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Rule candidate not found")
            previous_status = str(row["status"] or "candidate")
            new_status = next_status_by_action[action]
            self._conn.execute(
                """
                UPDATE rule_candidates
                SET status = ?,
                    last_review_action = ?,
                    last_review_reason = ?,
                    last_review_acted_by = ?,
                    last_review_source = ?,
                    last_review_at = ?,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    new_status,
                    action,
                    _clean_text(reason, 1000),
                    _clean_text(acted_by, 256) or "user",
                    _clean_text(source, 128) or "rule_candidate_operator_review",
                    now,
                    now,
                    candidate_id,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return RuleCandidateReviewActionResponse(
            candidate=self._row_to_candidate(updated),
            previous_status=previous_status,
            new_status=new_status,
            action=action,
        )

    def get_candidate(self, candidate_id: str) -> RuleCandidateRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Rule candidate not found")
        return self._row_to_candidate(row)

    def mark_promoted(
        self,
        candidate_id: str,
        *,
        law_id: str,
        reason: str,
        acted_by: str,
        source: str,
    ) -> RuleCandidateReviewActionResponse:
        now = _ts()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Rule candidate not found")
            previous_status = str(row["status"] or "candidate")
            self._conn.execute(
                """
                UPDATE rule_candidates
                SET status = 'suppressed',
                    promoted_law_id = ?,
                    promoted_at = ?,
                    last_review_action = 'promote',
                    last_review_reason = ?,
                    last_review_acted_by = ?,
                    last_review_source = ?,
                    last_review_at = ?,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    law_id,
                    now,
                    _clean_text(reason, 1000),
                    _clean_text(acted_by, 256) or "user",
                    _clean_text(source, 128) or "rule_candidate_promotion",
                    now,
                    now,
                    candidate_id,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return RuleCandidateReviewActionResponse(
            candidate=self._row_to_candidate(updated),
            previous_status=previous_status,
            new_status="suppressed",
            action="promote",
        )

    def mark_revision_pending(
        self,
        candidate_id: str,
        *,
        law_id: str,
        reason: str,
        acted_by: str,
        source: str,
    ) -> RuleCandidateReviewActionResponse:
        now = _ts()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Rule candidate not found")
            previous_status = str(row["status"] or "candidate")
            self._conn.execute(
                """
                UPDATE rule_candidates
                SET status = 'revision_pending',
                    revised_law_id = ?,
                    revised_at = ?,
                    related_rule_hint = ?,
                    last_review_action = 'revise_existing_law',
                    last_review_reason = ?,
                    last_review_acted_by = ?,
                    last_review_source = ?,
                    last_review_at = ?,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    law_id,
                    now,
                    law_id,
                    _clean_text(reason, 1000),
                    _clean_text(acted_by, 256) or "user",
                    _clean_text(source, 128) or "rule_candidate_law_revision",
                    now,
                    now,
                    candidate_id,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM rule_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return RuleCandidateReviewActionResponse(
            candidate=self._row_to_candidate(updated),
            previous_status=previous_status,
            new_status="revision_pending",
            action="revise_existing_law",
        )


_STORE: Optional[RuleLifecycleStore] = None


def get_rule_lifecycle_store() -> RuleLifecycleStore:
    global _STORE
    if _STORE is None:
        _STORE = RuleLifecycleStore()
    return _STORE


def close_rule_lifecycle_store() -> None:
    global _STORE
    if _STORE is not None:
        _STORE.close()
        _STORE = None


def project_rule_candidates_from_stenographer(
    *,
    store: RuleLifecycleStore | None = None,
    stenographer_store=None,
    project: str | None = None,
    limit: int = 500,
) -> RuleCandidateProjectionReport:
    lifecycle = store or get_rule_lifecycle_store()
    stenographer = stenographer_store or get_stenographer_store()
    since_ts = lifecycle.get_last_processed_timestamp()
    spans = stenographer.list_spans_after(
        since_ts=since_ts,
        kinds=RULE_MARKER_KINDS,
        project=project,
        limit=limit,
    )
    created: list[RuleCandidateRecord] = []
    errors: list[dict] = []
    skipped = 0
    last_seen = since_ts
    for span in spans:
        last_seen = max(last_seen, span.created_at.timestamp())
        try:
            payload = build_rule_candidate_from_span(span)
            candidate = lifecycle.create_candidate(payload)
            created.append(candidate)
        except RuleMarkerValidationError as exc:
            skipped += 1
            errors.append({"span_id": span.span_id, "error": str(exc)})
    if spans:
        lifecycle.set_last_processed_timestamp(last_seen)
    return RuleCandidateProjectionReport(
        scanned_spans=len(spans),
        created_candidates=len(created),
        skipped_spans=skipped,
        errors=errors,
        last_processed_timestamp=last_seen,
        candidates=created,
    )


_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "must",
    "not",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "this",
    "to",
    "with",
}


def _tokens(*values: str) -> set[str]:
    text = " ".join(str(value or "") for value in values).casefold()
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text)
        if token not in _STOPWORDS
    }


def _similarity(left: set[str], right: set[str], *, same_topic: bool = False) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right) / len(left | right)
    if same_topic:
        overlap += 0.15
    return min(1.0, round(overlap, 3))


def _recommendation(
    *,
    law_matches: list[RuleCandidateSimilarityMatch],
    candidate_matches: list[RuleCandidateSimilarityMatch],
) -> tuple[str, str]:
    if law_matches and law_matches[0].score >= 0.55:
        return (
            "revise_existing_law",
            "A similar active law already exists; review whether the candidate should revise or support it.",
        )
    if candidate_matches and candidate_matches[0].score >= 0.55:
        return (
            "merge_with_candidate",
            "A similar candidate already exists; consolidate before adding another rule.",
        )
    if law_matches or candidate_matches:
        return (
            "operator_review",
            "Related rules or candidates exist; operator review should decide promote, merge, or reject.",
        )
    return (
        "promote_candidate",
        "No strong overlap found; candidate may be suitable for promotion after operator review.",
    )


async def build_rule_candidate_review_packet(qdrant, body: RuleCandidateReviewRequest) -> RuleCandidateReviewPacket:
    store = get_rule_lifecycle_store()
    candidates = store.list_candidates(
        project=body.project,
        status=body.status,
        source_task_id=body.source_task_id,
        review_due=bool(body.review_due),
        limit=body.limit,
    )
    active_laws = await list_project_laws(
        qdrant,
        project=body.project,
        status="active",
        include_promoted=True,
        limit=200,
    )
    all_candidates = store.list_candidates(project=body.project, status=None, limit=500)
    items: list[RuleCandidateReviewItem] = []
    for candidate in candidates:
        candidate_tokens = _tokens(candidate.statement, candidate.rationale, candidate.topic_path)
        law_matches: list[RuleCandidateSimilarityMatch] = []
        for law in active_laws:
            score = _similarity(
                candidate_tokens,
                _tokens(law.title, law.statement, law.rationale, law.topic_path or ""),
                same_topic=bool(candidate.topic_path and candidate.topic_path == (law.topic_path or "")),
            )
            if score <= 0:
                continue
            law_matches.append(
                RuleCandidateSimilarityMatch(
                    match_type="active_law",
                    id=law.id,
                    title=law.title,
                    statement=law.statement,
                    status=law.status,
                    scope=law.scope,
                    topic_path=law.topic_path or "",
                    score=score,
                    reason="Deterministic token/topic overlap with active law.",
                )
            )
        candidate_matches: list[RuleCandidateSimilarityMatch] = []
        for other in all_candidates:
            if other.candidate_id == candidate.candidate_id:
                continue
            score = _similarity(
                candidate_tokens,
                _tokens(other.statement, other.rationale, other.topic_path),
                same_topic=bool(candidate.topic_path and candidate.topic_path == other.topic_path),
            )
            if score <= 0:
                continue
            candidate_matches.append(
                RuleCandidateSimilarityMatch(
                    match_type="rule_candidate",
                    id=other.candidate_id,
                    statement=other.statement,
                    status=other.status,
                    scope=other.scope,
                    topic_path=other.topic_path,
                    score=score,
                    reason="Deterministic token/topic overlap with another candidate.",
                )
            )
        law_matches.sort(key=lambda item: item.score, reverse=True)
        candidate_matches.sort(key=lambda item: item.score, reverse=True)
        recommendation, rationale = _recommendation(
            law_matches=law_matches,
            candidate_matches=candidate_matches,
        )
        items.append(
            RuleCandidateReviewItem(
                candidate=candidate,
                matching_laws=law_matches[: body.max_matches],
                matching_candidates=candidate_matches[: body.max_matches],
                recommendation=recommendation,
                rationale=rationale,
            )
        )
    return RuleCandidateReviewPacket(
        project=body.project,
        total_candidates=len(candidates),
        items=items,
        risk_controls=[
            "Review packet is read-only; it must not activate laws automatically.",
            "Merge or revise similar rules before promoting new candidates.",
            "Keep internal rule review text in English.",
        ],
        next_actions=[
            "Promote only candidates with no strong overlap after operator review.",
            "Use similar active laws as revision targets when overlap is high.",
            "Merge duplicate candidates before activation.",
        ],
    )


def _title_from_statement(statement: str) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", statement)
    title = " ".join(words[:8]).strip()
    return title[:256] or "Promoted Rule Candidate"


def _rule_semantic_key(*values: str) -> str:
    return " ".join(re.findall(r"[a-z0-9а-яё]+", " ".join(values).casefold()))


async def promote_rule_candidate(qdrant, ollama, candidate_id: str, body: RuleCandidatePromoteRequest) -> RuleCandidatePromoteResponse:
    store = get_rule_lifecycle_store()
    candidate = store.get_candidate(candidate_id)
    target_scope = body.target_scope or ("principle" if candidate.scope == "canonical_candidate" else "project")
    confirmed_by = (body.confirmed_by or "").strip() or ((body.acted_by or "").strip() if body.status in {"user_confirmed", "active"} else None)
    law = None
    promoted_law = None
    if candidate.promoted_law_id:
        try:
            promoted_law = await get_project_law(qdrant, candidate.promoted_law_id)
        except Exception:
            promoted_law = None
    candidate_key = _rule_semantic_key(candidate.statement)
    existing_laws = await list_project_laws(
        qdrant,
        project=candidate.project,
        status="all",
        include_promoted=True,
        limit=500,
    )
    semantic_matches = [
        item
        for item in existing_laws
        if _rule_semantic_key(item.statement) == candidate_key
        and item.id != (promoted_law.id if promoted_law is not None else "")
    ]
    semantic_matches.sort(
        key=lambda item: (
            0 if item.status == "active" else 1 if item.status == "user_confirmed" else 2 if item.status == "proposed" else 3,
            item.updated_at,
        )
    )
    preferred_existing = semantic_matches[0] if semantic_matches else None
    if promoted_law is not None and preferred_existing is not None and preferred_existing.status in {"active", "user_confirmed"}:
        if promoted_law.status not in {"superseded", "archived"}:
            await update_project_law_status(
                qdrant,
                promoted_law.id,
                status="superseded",
                reason=body.reason or f"Superseded by matching law {preferred_existing.id} during repeated candidate promotion.",
                acted_by=body.acted_by,
                action_source=body.source or "rule_candidate_promotion",
            )
        law = preferred_existing
    elif promoted_law is not None:
        law = promoted_law
    elif preferred_existing is not None and preferred_existing.status in {"active", "user_confirmed", "proposed"}:
        law = preferred_existing
    if law is None:
        law = await create_project_law(
            qdrant,
            ollama,
            ProjectLawCreate(
                project=candidate.project,
                title=body.title or _title_from_statement(candidate.statement),
                statement=candidate.statement,
                rationale=candidate.rationale,
                evidence=[
                    *candidate.evidence_refs,
                    f"rule_candidate:{candidate.candidate_id}",
                    f"stenographer_span:{candidate.source_span_id}",
                    body.reason,
                ],
                agent_id=body.acted_by,
                scope=target_scope,
                status=body.status,
                version="1.0",
                tags=["promoted_rule_candidate", f"rule_candidate:{candidate.candidate_id}"],
                topic_path=candidate.topic_path or None,
                confirmed_by=confirmed_by,
                confirmation_source=body.confirmation_source if confirmed_by else None,
            ),
        )
    elif body.status in {"user_confirmed", "active"} and confirmed_by and (not law.confirmed_by or law.status != body.status):
        law = await confirm_project_law(
            qdrant,
            ollama,
            law.id,
            ProjectLawConfirmRequest(
                confirmed_by=confirmed_by,
                confirmation_source=body.confirmation_source or body.source or "rule_candidate_promotion",
                reason=body.reason,
                activate=body.status == "active",
            ),
        )
    transition = store.mark_promoted(
        candidate_id,
        law_id=law.id,
        reason=body.reason,
        acted_by=body.acted_by,
        source=body.source,
    )
    return RuleCandidatePromoteResponse(
        candidate=transition.candidate,
        law=law.model_dump(mode="json"),
        previous_status=transition.previous_status,
        new_status=transition.new_status,
    )


async def revise_law_from_rule_candidate(
    qdrant,
    ollama,
    candidate_id: str,
    body: RuleCandidateReviseLawRequest,
) -> RuleCandidateReviseLawResponse:
    store = get_rule_lifecycle_store()
    candidate = store.get_candidate(candidate_id)
    current_law = await get_project_law(qdrant, body.law_id)
    candidate_evidence = [
        *candidate.evidence_refs,
        f"rule_candidate:{candidate.candidate_id}",
        f"stenographer_span:{candidate.source_span_id}",
        body.reason,
    ]
    evidence = body.evidence if body.evidence is not None else [*current_law.evidence, *candidate_evidence]
    updated_law = await update_project_law(
        qdrant,
        ollama,
        body.law_id,
        ProjectLawUpdate(
            title=body.title,
            statement=body.statement or candidate.statement,
            rationale=body.rationale if body.rationale is not None else candidate.rationale,
            evidence=evidence,
        ),
    )
    transition = store.mark_revision_pending(
        candidate_id,
        law_id=body.law_id,
        reason=body.reason,
        acted_by=body.acted_by,
        source=body.source,
    )
    return RuleCandidateReviseLawResponse(
        candidate=transition.candidate,
        law=updated_law.model_dump(mode="json"),
        previous_status=transition.previous_status,
        new_status=transition.new_status,
    )
