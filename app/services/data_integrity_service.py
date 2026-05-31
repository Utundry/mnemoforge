from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from qdrant_client.http import models as qmodels

from app.models.enums import MemoryType
from app.services.qdrant_rebuild_service import is_generic_memory_store_row
from app.services.system_data_root import data_path

logger = logging.getLogger(__name__)

_DB_PATH = data_path("integrity.db")
GENERIC_MEMORY_FILTER_SLICE_ID = "qdrant.generic_memory_filter"
SKILL_DOMAIN_TAGS_FILTER_SLICE_ID = "qdrant.skill_domain_tags_filter"
HANDOFF_STATUS_FILTER_SLICE_ID = "qdrant.handoff_status_filter"
CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID = "qdrant.code_component_language_filter"
DOC_SECTION_STATUS_FILTER_SLICE_ID = "qdrant.doc_section_status_filter"
TASK_MEMOIR_TAG_FILTER_SLICE_ID = "qdrant.task_memoir_tag_filter"
_HANDOFF_LIFECYCLE_STATUSES = ["pending", "picked_up", "active", "paused", "closed", "archived"]
_GENERIC_MEMORY_TYPE_VALUES = [item.value for item in MemoryType]
_GENERIC_MEMORY_EXCLUDED_CATEGORIES = ["handoff", "skill", "code_component", "doc_section", "task_memoir"]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS integrity_slices (
    slice_id         TEXT PRIMARY KEY,
    subsystem        TEXT NOT NULL,
    status           TEXT NOT NULL,
    severity         TEXT NOT NULL DEFAULT 'warning',
    source           TEXT NOT NULL DEFAULT '',
    degraded_since   REAL,
    last_checked_at  REAL NOT NULL,
    last_error       TEXT,
    details_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_integrity_status ON integrity_slices(status);
CREATE INDEX IF NOT EXISTS idx_integrity_subsystem ON integrity_slices(subsystem);
CREATE TABLE IF NOT EXISTS integrity_remediations (
    remediation_id  TEXT PRIMARY KEY,
    slice_id        TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    requested_by    TEXT NOT NULL DEFAULT '',
    job_id          TEXT,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    last_error      TEXT,
    details_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_integrity_rem_slice ON integrity_remediations(slice_id, status);
CREATE INDEX IF NOT EXISTS idx_integrity_rem_job ON integrity_remediations(job_id);
CREATE TABLE IF NOT EXISTS integrity_findings (
    finding_id       TEXT PRIMARY KEY,
    slice_id         TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT '',
    record_id        TEXT NOT NULL DEFAULT '',
    suspicion_type   TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0,
    status           TEXT NOT NULL DEFAULT 'suspect',
    source           TEXT NOT NULL DEFAULT '',
    first_seen_at    REAL NOT NULL,
    last_seen_at     REAL NOT NULL,
    details_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_integrity_find_slice ON integrity_findings(slice_id, status);
CREATE INDEX IF NOT EXISTS idx_integrity_find_record ON integrity_findings(record_id);
CREATE TABLE IF NOT EXISTS integrity_rules (
    rule_id          TEXT PRIMARY KEY,
    slice_id         TEXT NOT NULL,
    scope            TEXT NOT NULL DEFAULT 'slice',
    rule_type        TEXT NOT NULL DEFAULT 'guidance',
    priority         INTEGER NOT NULL DEFAULT 100,
    active           INTEGER NOT NULL DEFAULT 1,
    description      TEXT NOT NULL DEFAULT '',
    guidance_json    TEXT NOT NULL DEFAULT '{}',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integrity_rules_slice ON integrity_rules(slice_id, active, priority);
"""

REMEDIATION_REGISTRY: dict[str, list[dict[str, Any]]] = {
    GENERIC_MEMORY_FILTER_SLICE_ID: [
        {
            "action_type": "qdrant_reindex_from_sqlite",
            "job_type": "qdrant_reindex_from_sqlite",
            "payload": {"targets": ["memory"], "limit": 100},
            "description": "Rebuild the generic Qdrant memory slice from canonical SQLite-backed memory rows before re-auditing filters.",
        }
    ],
    SKILL_DOMAIN_TAGS_FILTER_SLICE_ID: [
        {
            "action_type": "qdrant_reindex_from_sqlite",
            "job_type": "qdrant_reindex_from_sqlite",
            "payload": {"targets": ["skill"], "limit": 100},
            "description": "Rebuild the Qdrant skill slice from canonical SQLite-backed skill records before re-auditing filters.",
        },
        {
            "action_type": "skills_retag",
            "job_type": "skills_retag",
            "payload": {"limit": 100},
            "description": "Retag legacy skills to normalize payload fields before re-auditing Qdrant skill filters.",
        }
    ],
    HANDOFF_STATUS_FILTER_SLICE_ID: [
        {
            "action_type": "qdrant_reindex_from_sqlite",
            "job_type": "qdrant_reindex_from_sqlite",
            "payload": {"targets": ["handoff"], "limit": 100},
            "description": "Rebuild the Qdrant handoff slice from canonical SQLite-backed handoff records before re-auditing filters.",
        },
        {
            "action_type": "handoff_repair_status",
            "job_type": "handoff_repair_status",
            "payload": {"limit": 100},
            "description": "Backfill invalid or missing handoff lifecycle statuses to restore status-based filters.",
        },
        {
            "action_type": "handoff_repair_target",
            "job_type": "handoff_repair_target",
            "payload": {"limit": 100},
            "description": "Recover missing handoff to:<agent> tags from payload metadata/content when possible.",
        }
    ],
    CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID: [
        {
            "action_type": "qdrant_reindex_from_sqlite",
            "job_type": "qdrant_reindex_from_sqlite",
            "payload": {"targets": ["code_component"], "limit": 100},
            "description": "Rebuild the Qdrant code_component slice from canonical SQLite-backed code chunks before re-auditing filters.",
        }
    ],
    DOC_SECTION_STATUS_FILTER_SLICE_ID: [
        {
            "action_type": "qdrant_reindex_from_sqlite",
            "job_type": "qdrant_reindex_from_sqlite",
            "payload": {"targets": ["doc_section"], "limit": 100},
            "description": "Rebuild the Qdrant doc_section slice from canonical SQLite-backed docs projections before re-auditing filters.",
        }
    ],
    TASK_MEMOIR_TAG_FILTER_SLICE_ID: [
        {
            "action_type": "qdrant_reindex_from_sqlite",
            "job_type": "qdrant_reindex_from_sqlite",
            "payload": {"targets": ["task_memoir"], "limit": 100},
            "description": "Rebuild the Qdrant task_memoir slice from canonical SQLite-backed memoir records before re-auditing filters.",
        }
    ],
}

SLICE_AUDIT_REGISTRY: dict[str, dict[str, Any]] = {
    GENERIC_MEMORY_FILTER_SLICE_ID: {
        "subsystem": "qdrant",
        "probe": "generic memory payload contract via memory_type filter with specialized categories excluded",
        "filter": qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="memory_type", match=qmodels.MatchAny(any=_GENERIC_MEMORY_TYPE_VALUES)),
            ],
            must_not=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchAny(any=_GENERIC_MEMORY_EXCLUDED_CATEGORIES)),
            ],
        ),
    },
    SKILL_DOMAIN_TAGS_FILTER_SLICE_ID: {
        "subsystem": "qdrant",
        "probe": "skill category + domain_tags match_any",
        "filter": qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
                qmodels.FieldCondition(key="domain_tags", match=qmodels.MatchAny(any=["python"])),
            ]
        ),
    },
    HANDOFF_STATUS_FILTER_SLICE_ID: {
        "subsystem": "qdrant",
        "probe": "handoff category + status match_any",
        "filter": qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="handoff")),
                qmodels.FieldCondition(key="status", match=qmodels.MatchAny(any=_HANDOFF_LIFECYCLE_STATUSES)),
            ]
        ),
    },
    CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID: {
        "subsystem": "qdrant",
        "probe": "code_component category + code_language match_any",
        "filter": qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="code_component")),
                qmodels.FieldCondition(key="code_language", match=qmodels.MatchAny(any=["python"])),
            ]
        ),
    },
    DOC_SECTION_STATUS_FILTER_SLICE_ID: {
        "subsystem": "qdrant",
        "probe": "doc_section category + status match_any",
        "filter": qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="doc_section")),
                qmodels.FieldCondition(key="status", match=qmodels.MatchAny(any=["active"])),
            ]
        ),
    },
    TASK_MEMOIR_TAG_FILTER_SLICE_ID: {
        "subsystem": "qdrant",
        "probe": "task_memoir category + tags memoir match",
        "filter": qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
                qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value="memoir")),
            ]
        ),
    },
}

TARGETED_REPAIR_EXECUTORS: dict[str, dict[str, Any]] = {
    "qdrant_reindex_from_sqlite": {
        "job_type": "qdrant_reindex_from_sqlite",
        "supports_record_ids": True,
        "description": "Rebuild the affected Qdrant slice from canonical SQLite-backed records.",
    },
    "skills_retag": {
        "job_type": "skills_retag",
        "supports_record_ids": True,
        "description": "Retag a targeted batch of suspect skill records.",
    },
    "handoff_repair_status": {
        "job_type": "handoff_repair_status",
        "supports_record_ids": True,
        "description": "Backfill invalid or missing handoff statuses for targeted records.",
    },
    "handoff_repair_target": {
        "job_type": "handoff_repair_target",
        "supports_record_ids": True,
        "description": "Recover missing handoff target tags for targeted records.",
    },
}


def _extract_fixed_record_ids(action_type: str, job_result: dict[str, Any]) -> list[str]:
    if action_type == "qdrant_reindex_from_sqlite":
        return [str(item) for item in (job_result.get("upserted_ids") or []) if item]
    if action_type == "skills_retag":
        return [str(item.get("id")) for item in job_result.get("details", []) if item.get("id")]
    if action_type == "handoff_repair_status":
        return [str(item) for item in (job_result.get("fixed_ids") or []) if item]
    if action_type == "handoff_repair_target":
        return [str(item) for item in (job_result.get("fixed_ids") or []) if item]
    return []


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["details"] = json.loads(data.get("details_json") or "{}")
    data.pop("details_json", None)
    if "guidance_json" in data:
        data["guidance"] = json.loads(data.get("guidance_json") or "{}")
        data.pop("guidance_json", None)
    return data


class DataIntegrityStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    def upsert_slice(
        self,
        *,
        slice_id: str,
        subsystem: str,
        status: str,
        severity: str = "warning",
        source: str = "",
        error: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            current = self._conn.execute(
                "SELECT degraded_since FROM integrity_slices WHERE slice_id = ?",
                (slice_id,),
            ).fetchone()
            degraded_since = current["degraded_since"] if current else None
            if status == "degraded" and degraded_since is None:
                degraded_since = now
            if status == "healthy":
                degraded_since = None
            self._conn.execute(
                """
                INSERT INTO integrity_slices (
                    slice_id, subsystem, status, severity, source, degraded_since,
                    last_checked_at, last_error, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slice_id) DO UPDATE SET
                    subsystem=excluded.subsystem,
                    status=excluded.status,
                    severity=excluded.severity,
                    source=excluded.source,
                    degraded_since=excluded.degraded_since,
                    last_checked_at=excluded.last_checked_at,
                    last_error=excluded.last_error,
                    details_json=excluded.details_json
                """,
                (
                    slice_id,
                    subsystem,
                    status,
                    severity,
                    source,
                    degraded_since,
                    now,
                    error or None,
                    json.dumps(details or {}),
                ),
            )
            self._conn.commit()

    def list_slices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM integrity_slices ORDER BY status DESC, last_checked_at DESC"
            ).fetchall()
        return [_decode(row) for row in rows]

    def get_slice(self, slice_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM integrity_slices WHERE slice_id = ?",
                (slice_id,),
            ).fetchone()
        return _decode(row) if row else None

    def patch_slice_details(self, *, slice_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT details_json FROM integrity_slices WHERE slice_id = ?",
                (slice_id,),
            ).fetchone()
            if row is None:
                return None
            details = json.loads(row["details_json"] or "{}")
            details.update(patch)
            self._conn.execute(
                "UPDATE integrity_slices SET details_json = ? WHERE slice_id = ?",
                (json.dumps(details), slice_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM integrity_slices WHERE slice_id = ?",
                (slice_id,),
            ).fetchone()
        return _decode(updated) if updated else None

    def overview(self) -> dict[str, Any]:
        items = self.list_slices()
        degraded = [item for item in items if item.get("status") == "degraded"]
        remediations = self.list_remediations(limit=200)
        active_remediations = [
            item for item in remediations if item.get("status") in {"queued", "running"}
        ]
        suspect_findings = self.findings_summary()
        actionable_slices = sorted(
            {
                *(item["slice_id"] for item in degraded),
                *suspect_findings.get("by_slice", {}).keys(),
            }
        )
        return {
            "status": "degraded" if degraded else "ok",
            "degraded_count": len(degraded),
            "degraded_slices": [item["slice_id"] for item in degraded],
            "actionable_slices": actionable_slices,
            "recommended_remediations": {
                slice_id: self.recommended_remediations(slice_id)
                for slice_id in actionable_slices
            },
            "active_remediations": active_remediations,
            "suspect_findings": suspect_findings,
            "slices": items,
        }

    def is_degraded(self, slice_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM integrity_slices WHERE slice_id = ?",
                (slice_id,),
            ).fetchone()
        return bool(row and row["status"] == "degraded")

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM integrity_slices")
            self._conn.execute("DELETE FROM integrity_remediations")
            self._conn.execute("DELETE FROM integrity_findings")
            self._conn.execute("DELETE FROM integrity_rules")
            self._conn.commit()

    def recommended_remediations(self, slice_id: str) -> list[dict[str, Any]]:
        recommendations = [dict(item) for item in REMEDIATION_REGISTRY.get(slice_id, [])]
        recent = self.list_remediations(slice_id=slice_id, limit=20)
        done = [item for item in recent if item.get("status") == "done"]
        if done and self.is_degraded(slice_id):
            latest_done = max(done, key=lambda item: item.get("finished_at") or item.get("created_at") or 0.0)
            closure = (latest_done.get("details") or {}).get("closure_summary") or {}
            repaired_findings = int(closure.get("repaired_findings") or 0)
            recommendations.insert(
                0,
                {
                    "action_type": "manual_forensic_audit",
                    "job_type": "",
                    "payload": {
                        "slice_id": slice_id,
                        "prior_action_type": latest_done.get("action_type", ""),
                        "prior_remediation_id": latest_done.get("remediation_id", ""),
                    },
                    "description": (
                        "A prior remediation completed but the slice is still degraded. "
                        "Perform manual/forensic audit of suspect records or schema drift before repeating automated repair."
                    ),
                    "escalated_after": latest_done.get("action_type", ""),
                    "repaired_findings": repaired_findings,
                },
            )
        return recommendations

    def list_remediations(
        self,
        *,
        slice_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM integrity_remediations WHERE 1=1"
        params: list[Any] = []
        if slice_id:
            sql += " AND slice_id = ?"
            params.append(slice_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode(row) for row in rows]

    def queue_remediation(
        self,
        *,
        remediation_id: str,
        slice_id: str,
        action_type: str,
        requested_by: str,
        job_id: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT * FROM integrity_remediations
                WHERE slice_id = ? AND action_type = ? AND status IN ('queued','running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (slice_id, action_type),
            ).fetchone()
            if existing:
                return _decode(existing)
            self._conn.execute(
                """
                INSERT INTO integrity_remediations(
                    remediation_id, slice_id, action_type, status, requested_by, job_id, created_at, details_json
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    remediation_id,
                    slice_id,
                    action_type,
                    requested_by,
                    job_id,
                    now,
                    json.dumps(details or {}),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM integrity_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
        return _decode(row)

    def sync_remediation_status(self, *, remediation_id: str, status: str, error: str = "") -> None:
        now = time.time()
        started_at = now if status == "running" else None
        finished_at = now if status in {"done", "failed"} else None
        with self._lock:
            if status == "running":
                self._conn.execute(
                    """
                    UPDATE integrity_remediations
                    SET status = ?, started_at = COALESCE(started_at, ?), last_error = NULL
                    WHERE remediation_id = ?
                    """,
                    (status, started_at, remediation_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE integrity_remediations
                    SET status = ?, finished_at = ?, last_error = ?
                    WHERE remediation_id = ?
                    """,
                    (status, finished_at, error or None, remediation_id),
                )
            self._conn.commit()

    def sync_remediations_from_jobs(self, jobs: list[dict[str, Any]]) -> int:
        changed = 0
        by_job_id = {job["id"]: job for job in jobs}
        active = self.list_remediations(status=None, limit=500)
        for item in active:
            job_id = item.get("job_id")
            if not job_id:
                continue
            job = by_job_id.get(job_id)
            if not job:
                continue
            current = item.get("status")
            target = None
            if job.get("status") == "running" and current == "queued":
                target = "running"
            elif job.get("status") == "done" and current not in {"done", "failed"}:
                target = "done"
            elif job.get("status") == "failed" and current != "failed":
                target = "failed"
            if target:
                self.sync_remediation_status(
                    remediation_id=item["remediation_id"],
                    status=target,
                    error=str(job.get("error", "")),
                )
                changed += 1
        return changed

    def get_remediation(self, remediation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM integrity_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
        return _decode(row) if row else None

    def upsert_finding(
        self,
        *,
        finding_id: str,
        slice_id: str,
        category: str,
        record_id: str,
        suspicion_type: str,
        confidence: float,
        source: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT first_seen_at, status FROM integrity_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            first_seen_at = existing["first_seen_at"] if existing else now
            preserved_status = existing["status"] if existing else "suspect"
            self._conn.execute(
                """
                INSERT INTO integrity_findings(
                    finding_id, slice_id, category, record_id, suspicion_type, confidence,
                    status, source, first_seen_at, last_seen_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(finding_id) DO UPDATE SET
                    slice_id=excluded.slice_id,
                    category=excluded.category,
                    record_id=excluded.record_id,
                    suspicion_type=excluded.suspicion_type,
                    confidence=excluded.confidence,
                    status=excluded.status,
                    source=excluded.source,
                    first_seen_at=excluded.first_seen_at,
                    last_seen_at=excluded.last_seen_at,
                    details_json=excluded.details_json
                """,
                (
                    finding_id,
                    slice_id,
                    category,
                    record_id,
                    suspicion_type,
                    confidence,
                    preserved_status,
                    source,
                    first_seen_at,
                    now,
                    json.dumps(details or {}),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM integrity_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        return _decode(row)

    def list_findings(
        self,
        *,
        slice_id: str | None = None,
        record_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM integrity_findings WHERE 1=1"
        params: list[Any] = []
        if slice_id:
            sql += " AND slice_id = ?"
            params.append(slice_id)
        if record_id:
            sql += " AND record_id = ?"
            params.append(record_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY confidence DESC, last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode(row) for row in rows]

    def findings_summary(self) -> dict[str, Any]:
        items = self.list_findings(limit=500)
        active = [item for item in items if item.get("status") in {"suspect", "quarantine_candidate", "quarantined"}]
        by_slice: dict[str, int] = {}
        for item in active:
            by_slice[item["slice_id"]] = by_slice.get(item["slice_id"], 0) + 1
        return {
            "active_count": len(active),
            "by_slice": by_slice,
        }

    def set_finding_status(self, *, finding_id: str, status: str) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute(
                "UPDATE integrity_findings SET status = ?, last_seen_at = ? WHERE finding_id = ?",
                (status, time.time(), finding_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM integrity_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        return _decode(row) if row else None

    def patch_remediation_details(self, *, remediation_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT details_json FROM integrity_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
            if row is None:
                return None
            details = json.loads(row["details_json"] or "{}")
            details.update(patch)
            self._conn.execute(
                "UPDATE integrity_remediations SET details_json = ? WHERE remediation_id = ?",
                (json.dumps(details), remediation_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM integrity_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
        return _decode(updated) if updated else None

    def upsert_rule(
        self,
        *,
        rule_id: str | None = None,
        slice_id: str,
        description: str,
        guidance: dict[str, Any] | None = None,
        scope: str = "slice",
        rule_type: str = "guidance",
        priority: int = 100,
        active: bool = True,
    ) -> dict[str, Any]:
        now = time.time()
        rule_id = rule_id or str(uuid4())
        guidance = guidance or {}
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO integrity_rules(
                    rule_id, slice_id, scope, rule_type, priority, active, description, guidance_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    slice_id=excluded.slice_id,
                    scope=excluded.scope,
                    rule_type=excluded.rule_type,
                    priority=excluded.priority,
                    active=excluded.active,
                    description=excluded.description,
                    guidance_json=excluded.guidance_json,
                    updated_at=excluded.updated_at
                """,
                (
                    rule_id,
                    slice_id,
                    scope,
                    rule_type,
                    priority,
                    1 if active else 0,
                    description,
                    json.dumps(guidance),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM integrity_rules WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()
        item = _decode(row)
        item["active"] = bool(item.get("active"))
        return item

    def list_rules(
        self,
        *,
        slice_id: str | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM integrity_rules WHERE 1=1"
        params: list[Any] = []
        if slice_id:
            sql += " AND slice_id = ?"
            params.append(slice_id)
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY priority ASC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        items = [_decode(row) for row in rows]
        for item in items:
            item["active"] = bool(item.get("active"))
        return items

    def close(self) -> None:
        with self._lock:
            self._conn.close()


async def run_integrity_audit(qdrant) -> dict[str, Any]:
    from app.config import settings

    store = get_data_integrity_store()
    checks: list[dict[str, Any]] = []

    for slice_id, spec in SLICE_AUDIT_REGISTRY.items():
        probe = str(spec.get("probe") or "")
        try:
            await qdrant._client.scroll(
                collection_name=settings.qdrant_collection_name,
                scroll_filter=spec["filter"],
                limit=5,
                with_payload=False,
                with_vectors=False,
            )
            store.upsert_slice(
                slice_id=slice_id,
                subsystem=str(spec.get("subsystem") or "qdrant"),
                status="healthy",
                source="background_audit",
                details={"probe": probe},
            )
            checks.append({"slice_id": slice_id, "status": "healthy"})
        except Exception as e:
            logger.warning("Integrity audit detected degraded slice %s: %s", slice_id, e)
            store.upsert_slice(
                slice_id=slice_id,
                subsystem=str(spec.get("subsystem") or "qdrant"),
                status="degraded",
                source="background_audit",
                error=str(e),
                details={"probe": probe},
            )
            checks.append(
                {
                    "slice_id": slice_id,
                    "status": "degraded",
                    "error": str(e),
                }
            )

    overview = store.overview()
    return {
        "status": overview["status"],
        "degraded_count": overview["degraded_count"],
        "checks": checks,
    }


async def queue_recommended_remediation(
    *,
    slice_id: str,
    requested_by: str,
    queue,
    discover_if_needed: bool = False,
    discovery_limit: int = 50,
) -> dict[str, Any]:
    def _select_recommended_option(
        *,
        slice_id: str,
        options: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        actionable = [item for item in options if str(item.get("job_type") or "").strip()]
        if not actionable:
            return None, None
        try:
            plan = build_integrity_repair_plan(slice_id, limit=50)
            for action in plan.get("actions", []):
                action_type = str(action.get("action_type") or "").strip()
                if not action_type:
                    continue
                selected = next(
                    (item for item in actionable if str(item.get("action_type") or "").strip() == action_type),
                    None,
                )
                if selected is not None:
                    return selected, action
        except Exception as exc:
            logger.debug("Failed to derive recommendation from repair plan for %s: %s", slice_id, exc)
        return actionable[0], None

    store = get_data_integrity_store()
    if discover_if_needed:
        try:
            discovery = await maybe_auto_discover_slice(
                slice_id,
                limit=max(1, discovery_limit),
                cooldown_seconds=0.0,
            )
            if discovery.get("performed"):
                logger.info(
                    "On-demand integrity discovery completed before remediation selection: slice=%s discovered=%d",
                    slice_id,
                    discovery.get("discovered", 0),
                )
        except Exception as exc:
            logger.warning("On-demand integrity discovery failed for %s before remediation selection: %s", slice_id, exc)
    options = store.recommended_remediations(slice_id)
    if not options:
        raise ValueError(f"No remediation is registered for slice {slice_id}")
    selected, planned_action = _select_recommended_option(slice_id=slice_id, options=options)
    if selected is None:
        raise ValueError(
            f"No background remediation is available for slice {slice_id}; manual operator action is required."
        )
    action_type = str(selected.get("action_type") or "")
    payload = dict(selected.get("payload") or {})
    executor = TARGETED_REPAIR_EXECUTORS.get(action_type)
    if planned_action and executor and executor.get("supports_record_ids"):
        record_ids = [str(item) for item in (planned_action.get("record_ids") or []) if item]
        if record_ids:
            payload["record_ids"] = record_ids
    job_type = str(selected.get("job_type") or "").strip()
    if not job_type:
        raise ValueError(
            f"Recommended remediation '{action_type}' for slice {slice_id} has no background executor."
        )
    if job_type == "qdrant_reindex_from_sqlite":
        try:
            # Defensive bootstrap for cases where queue was re-created without startup registration.
            from app.services.qdrant_rebuild_service import register_qdrant_reindex_job_handler

            register_qdrant_reindex_job_handler(queue)
        except Exception as exc:
            logger.warning("Failed to ensure qdrant_reindex_from_sqlite handler before queue submit: %s", exc)
    if job_type == "skills_retag":
        try:
            # Defensive bootstrap for cases where queue was re-created without startup registration.
            from app.routers.skills import _retag_handler

            queue.register("skills_retag", _retag_handler)
        except Exception as exc:
            logger.warning("Failed to ensure skills_retag handler before queue submit: %s", exc)
    job_id = await queue.submit(job_type, payload)
    import uuid

    remediation_id = str(uuid.uuid4())
    details = {
        "description": selected.get("description", ""),
        "payload": payload,
    }
    if planned_action is not None:
        details["source"] = "recommended_by_findings"
        details["finding_count"] = int(planned_action.get("finding_count") or 0)
    return store.queue_remediation(
        remediation_id=remediation_id,
        slice_id=slice_id,
        action_type=action_type,
        requested_by=requested_by,
        job_id=job_id,
        details=details,
    )


def _skill_suspicions_for_row(row: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    meta = row.get("metadata", {})
    record_id = row["memory_id"]
    content = row.get("content", "")
    name = meta.get("skill_name", "")
    domain_tags = meta.get("domain_tags")
    description = meta.get("description", "")
    issues: list[tuple[str, float, dict[str, Any]]] = []
    if not isinstance(domain_tags, list) or not domain_tags:
        issues.append(
            (
                "missing_domain_tags",
                0.85,
                {"suggested_repair": "skills_retag", "reason": "domain_tags empty or malformed"},
            )
        )
    if not name or name == "unknown":
        issues.append(
            (
                "unknown_skill_name",
                0.8,
                {"suggested_repair": "skills_retag", "reason": "skill_name missing or unknown"},
            )
        )
    if len(content.strip()) < 20:
        issues.append(
            (
                "truncated_skill_content",
                0.6,
                {"suggested_repair": "regenerate_or_review", "reason": "skill content is unusually short"},
            )
        )
    if description == "" and len(content.strip()) >= 20:
        issues.append(
            (
                "missing_description",
                0.55,
                {"suggested_repair": "skills_retag", "reason": "description missing in SQLite metadata"},
            )
        )
    return issues


async def _discover_skill_slice_findings(
    *,
    store: DataIntegrityStore,
    slice_id: str,
    limit: int,
    record_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from app.services.memory_store import get_memory_store

    rows = await get_memory_store().list_by_category("skill", limit=limit)
    if record_ids is not None:
        wanted = set(record_ids)
        rows = [row for row in rows if row["memory_id"] in wanted]
    findings: list[dict[str, Any]] = []
    for row in rows:
        record_id = row["memory_id"]
        meta = row.get("metadata", {})
        name = meta.get("skill_name", "")
        issues = _skill_suspicions_for_row(row)
        for suspicion_type, confidence, details in issues:
            finding = store.upsert_finding(
                finding_id=f"{slice_id}:{record_id}:{suspicion_type}",
                slice_id=slice_id,
                category="skill",
                record_id=record_id,
                suspicion_type=suspicion_type,
                confidence=confidence,
                source="heuristic_discovery",
                details={
                    **details,
                    "record_id": record_id,
                    "skill_name": name or "unknown",
                },
            )
            findings.append(finding)
    return findings


def _generic_memory_suspicions_for_row(row: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    metadata = dict(row.get("metadata") or {})
    issues: list[tuple[str, float, dict[str, Any]]] = []
    memory_type = str(metadata.get("memory_type") or "").strip()
    agent_id = str(metadata.get("agent_id") or "").strip()
    source = str(metadata.get("source") or "").strip()
    timestamp = str(metadata.get("timestamp") or "").strip()
    category = str(metadata.get("category") or "").strip()

    if memory_type not in _GENERIC_MEMORY_TYPE_VALUES:
        issues.append(
            (
                "invalid_memory_type",
                0.85,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": f"generic memory row has invalid memory_type '{memory_type or 'missing'}'",
                },
            )
        )
    if not agent_id:
        issues.append(
            (
                "missing_agent_id",
                0.75,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "generic memory row has no agent_id",
                },
            )
        )
    if not source:
        issues.append(
            (
                "missing_source",
                0.7,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "generic memory row has no source",
                },
            )
        )
    if not timestamp:
        issues.append(
            (
                "missing_timestamp",
                0.7,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "generic memory row has no timestamp",
                },
            )
        )
    if not category:
        issues.append(
            (
                "missing_category",
                0.8,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "generic memory row has no category",
                },
            )
        )
    return issues


async def _discover_generic_memory_slice_findings(
    *,
    store: DataIntegrityStore,
    slice_id: str,
    limit: int,
    record_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from app.services.memory_store import get_memory_store

    rows = await get_memory_store().list_by_category("memory", limit=max(limit * 5, 500))
    if record_ids is not None:
        wanted = set(record_ids)
        rows = [row for row in rows if row.get("memory_id") in wanted]
    findings: list[dict[str, Any]] = []
    for row in rows:
        if not is_generic_memory_store_row(row):
            continue
        record_id = str(row.get("memory_id") or "")
        metadata = dict(row.get("metadata") or {})
        issues = _generic_memory_suspicions_for_row(row)
        for suspicion_type, confidence, details in issues:
            finding = store.upsert_finding(
                finding_id=f"{slice_id}:{record_id}:{suspicion_type}",
                slice_id=slice_id,
                category="memory",
                record_id=record_id,
                suspicion_type=suspicion_type,
                confidence=confidence,
                source="heuristic_discovery",
                details={
                    **details,
                    "record_id": record_id,
                    "category": str(metadata.get("category") or ""),
                    "memory_type": str(metadata.get("memory_type") or ""),
                },
            )
            findings.append(finding)
    return findings


def _code_component_suspicions_for_row(row: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    metadata = dict(row.get("metadata") or {})
    issues: list[tuple[str, float, dict[str, Any]]] = []
    if not str(metadata.get("code_path") or "").strip():
        issues.append(
            (
                "missing_code_path",
                0.85,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "code_component row has no code_path",
                },
            )
        )
    if not str(metadata.get("code_language") or "").strip():
        issues.append(
            (
                "missing_code_language",
                0.85,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "code_component row has no code_language",
                },
            )
        )
    return issues


async def _discover_code_component_slice_findings(
    *,
    store: DataIntegrityStore,
    slice_id: str,
    limit: int,
    record_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from app.services.memory_store import get_memory_store

    rows = await get_memory_store().list_by_category("code_component", limit=max(limit, 200))
    if record_ids is not None:
        wanted = set(record_ids)
        rows = [row for row in rows if row.get("memory_id") in wanted]
    findings: list[dict[str, Any]] = []
    for row in rows:
        record_id = str(row.get("memory_id") or "")
        metadata = dict(row.get("metadata") or {})
        for suspicion_type, confidence, details in _code_component_suspicions_for_row(row):
            findings.append(
                store.upsert_finding(
                    finding_id=f"{slice_id}:{record_id}:{suspicion_type}",
                    slice_id=slice_id,
                    category="code_component",
                    record_id=record_id,
                    suspicion_type=suspicion_type,
                    confidence=confidence,
                    source="heuristic_discovery",
                    details={**details, "record_id": record_id, "code_path": str(metadata.get("code_path") or "")},
                )
            )
    return findings


def _doc_section_suspicions_for_row(row: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    metadata = dict(row.get("metadata") or {})
    issues: list[tuple[str, float, dict[str, Any]]] = []
    if str(metadata.get("status") or "").strip() != "active":
        issues.append(
            (
                "invalid_doc_section_status",
                0.8,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "doc_section row should carry status=active for projection filters",
                },
            )
        )
    if not str(metadata.get("project") or "").strip():
        issues.append(
            (
                "missing_doc_section_project",
                0.75,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "doc_section row has no project",
                },
            )
        )
    return issues


async def _discover_doc_section_slice_findings(
    *,
    store: DataIntegrityStore,
    slice_id: str,
    limit: int,
    record_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from app.services.memory_store import get_memory_store

    rows = await get_memory_store().list_by_category("doc_section", limit=max(limit, 200))
    if record_ids is not None:
        wanted = set(record_ids)
        rows = [row for row in rows if row.get("memory_id") in wanted]
    findings: list[dict[str, Any]] = []
    for row in rows:
        record_id = str(row.get("memory_id") or "")
        metadata = dict(row.get("metadata") or {})
        for suspicion_type, confidence, details in _doc_section_suspicions_for_row(row):
            findings.append(
                store.upsert_finding(
                    finding_id=f"{slice_id}:{record_id}:{suspicion_type}",
                    slice_id=slice_id,
                    category="doc_section",
                    record_id=record_id,
                    suspicion_type=suspicion_type,
                    confidence=confidence,
                    source="heuristic_discovery",
                    details={**details, "record_id": record_id, "project": str(metadata.get("project") or "")},
                )
            )
    return findings


def _task_memoir_suspicions_for_row(row: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    metadata = dict(row.get("metadata") or {})
    issues: list[tuple[str, float, dict[str, Any]]] = []
    tags = [str(item) for item in (metadata.get("tags") or [])]
    if "memoir" not in tags:
        issues.append(
            (
                "missing_memoir_tag",
                0.8,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "task_memoir row is missing the memoir tag required by the Qdrant slice",
                },
            )
        )
    meta = metadata.get("meta") or {}
    if not str(meta.get("task_id") or "").strip():
        issues.append(
            (
                "missing_task_id",
                0.75,
                {
                    "suggested_repair": "qdrant_reindex_from_sqlite",
                    "reason": "task_memoir row has no task_id in metadata",
                },
            )
        )
    return issues


async def _discover_task_memoir_slice_findings(
    *,
    store: DataIntegrityStore,
    slice_id: str,
    limit: int,
    record_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from app.services.memory_store import get_memory_store

    rows = await get_memory_store().list_by_category("task_memoir", limit=max(limit, 200))
    if record_ids is not None:
        wanted = set(record_ids)
        rows = [row for row in rows if row.get("memory_id") in wanted]
    findings: list[dict[str, Any]] = []
    for row in rows:
        record_id = str(row.get("memory_id") or "")
        metadata = dict(row.get("metadata") or {})
        for suspicion_type, confidence, details in _task_memoir_suspicions_for_row(row):
            findings.append(
                store.upsert_finding(
                    finding_id=f"{slice_id}:{record_id}:{suspicion_type}",
                    slice_id=slice_id,
                    category="task_memoir",
                    record_id=record_id,
                    suspicion_type=suspicion_type,
                    confidence=confidence,
                    source="heuristic_discovery",
                    details={**details, "record_id": record_id, "project": str(metadata.get("project") or "")},
                )
            )
    return findings


def _handoff_suspicions_for_payload(payload: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    issues: list[tuple[str, float, dict[str, Any]]] = []
    status = str(payload.get("status") or "").strip()
    tags = [str(tag) for tag in (payload.get("tags") or [])]

    if not status:
        issues.append(
            (
                "missing_handoff_status",
                0.85,
                {"suggested_repair": "handoff_repair_status", "reason": "handoff payload has no lifecycle status"},
            )
        )
    elif status not in _HANDOFF_LIFECYCLE_STATUSES:
        issues.append(
            (
                "unknown_handoff_status",
                0.75,
                {
                    "suggested_repair": "handoff_repair_status",
                    "reason": f"handoff lifecycle status '{status}' is not recognized",
                },
            )
        )

    if not any(tag.startswith("to:") for tag in tags):
        issues.append(
            (
                "missing_handoff_target",
                0.7,
                {"suggested_repair": "handoff_repair_target", "reason": "handoff payload has no to:<agent> tag"},
            )
        )
    return issues


async def _discover_handoff_slice_findings(
    *,
    store: DataIntegrityStore,
    slice_id: str,
    limit: int,
    record_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from app.config import settings
    from app.dependencies import get_qdrant
    from app.services.memory_store import get_memory_store

    qdrant = get_qdrant()
    scan_limit = max(limit, 100)
    points: list[dict[str, Any]] = []
    try:
        results, _ = await qdrant._client.scroll(
            collection_name=settings.qdrant_collection_name,
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="handoff")),
                ]
            ),
            limit=scan_limit,
            with_payload=True,
            with_vectors=False,
        )
        points = [{"id": str(item.id), "payload": dict(item.payload or {})} for item in results]
    except Exception as exc:
        logger.warning(
            "Handoff integrity discovery scroll failed, falling back to SQLite memory store: %s",
            exc,
        )
        store.upsert_slice(
            slice_id=slice_id,
            subsystem="qdrant",
            status="degraded",
            source="integrity_discovery",
            error=str(exc),
            details={"fallback": "sqlite_memory_store", "detector": "handoff"},
        )
        rows = await get_memory_store().list_by_category("memory", limit=max(scan_limit * 5, 500))
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            if metadata.get("category") != "handoff":
                continue
            points.append(
                {
                    "id": str(row.get("memory_id") or ""),
                    "payload": metadata,
                }
            )
    if record_ids is not None:
        wanted = set(record_ids)
        points = [item for item in points if str(item.get("id") or "") in wanted]
    findings: list[dict[str, Any]] = []
    for point in points:
        payload = dict(point.get("payload") or {})
        record_id = str(point.get("id") or "")
        issues = _handoff_suspicions_for_payload(payload)
        for suspicion_type, confidence, details in issues:
            finding = store.upsert_finding(
                finding_id=f"{slice_id}:{record_id}:{suspicion_type}",
                slice_id=slice_id,
                category="handoff",
                record_id=record_id,
                suspicion_type=suspicion_type,
                confidence=confidence,
                source="heuristic_discovery",
                details={**details, "record_id": record_id},
            )
            findings.append(finding)
    return findings


DISCOVERY_DETECTORS = {
    GENERIC_MEMORY_FILTER_SLICE_ID: _discover_generic_memory_slice_findings,
    SKILL_DOMAIN_TAGS_FILTER_SLICE_ID: _discover_skill_slice_findings,
    HANDOFF_STATUS_FILTER_SLICE_ID: _discover_handoff_slice_findings,
    CODE_COMPONENT_LANGUAGE_FILTER_SLICE_ID: _discover_code_component_slice_findings,
    DOC_SECTION_STATUS_FILTER_SLICE_ID: _discover_doc_section_slice_findings,
    TASK_MEMOIR_TAG_FILTER_SLICE_ID: _discover_task_memoir_slice_findings,
}


async def discover_suspect_records(
    slice_id: str,
    *,
    limit: int = 500,
    record_ids: list[str] | None = None,
) -> dict[str, Any]:
    store = get_data_integrity_store()
    detector = DISCOVERY_DETECTORS.get(slice_id)
    if detector is None:
        raise ValueError(f"No discovery detector is registered for slice {slice_id}")
    findings = await detector(
        store=store,
        slice_id=slice_id,
        limit=limit,
        record_ids=record_ids,
    )

    return {
        "slice_id": slice_id,
        "discovered": len(findings),
        "findings": findings,
    }


async def reconcile_completed_remediations(*, queue) -> dict[str, Any]:
    store = get_data_integrity_store()
    remediations = store.list_remediations(status="done", limit=500)
    reconciled = 0
    repaired = 0

    for remediation in remediations:
        details = remediation.get("details", {})
        if details.get("closure_checked_at"):
            continue

        job_id = remediation.get("job_id")
        job = queue.get_job(job_id) if job_id else None
        job_result = (job or {}).get("result") or {}
        slice_id = remediation["slice_id"]
        action_type = remediation["action_type"]
        requested_record_ids = [
            str(item)
            for item in ((details.get("payload") or {}).get("record_ids") or [])
            if item
        ]

        fixed_ids = _extract_fixed_record_ids(action_type, job_result)

        remediation_repaired = 0
        unresolved_record_ids: list[str] = list(requested_record_ids)
        if fixed_ids:
            discovery = await discover_suspect_records(slice_id, limit=max(len(fixed_ids), 10), record_ids=fixed_ids)
            current_discovered = discovery.get("findings", [])
            current_pairs = {
                (item["record_id"], item["suspicion_type"])
                for item in current_discovered
            }
            current_findings = store.list_findings(slice_id=slice_id, limit=5000)
            existing = [
                item for item in current_findings
                if item["record_id"] in fixed_ids and item["status"] in {"suspect", "quarantine_candidate", "quarantined", "repaired"}
            ]
            for finding in existing:
                pair = (finding["record_id"], finding["suspicion_type"])
                if pair not in current_pairs and finding["status"] != "repaired":
                    store.set_finding_status(finding_id=finding["finding_id"], status="repaired")
                    repaired += 1
                    remediation_repaired += 1
                elif pair in current_pairs and finding["status"] == "repaired":
                    store.set_finding_status(finding_id=finding["finding_id"], status="suspect")
            unresolved_record_ids = sorted({item["record_id"] for item in current_discovered})
        store.patch_remediation_details(
            remediation_id=remediation["remediation_id"],
            patch={
                "closure_checked_at": time.time(),
                "closure_summary": {
                    "targeted_record_ids": requested_record_ids,
                    "fixed_ids": fixed_ids,
                    "unresolved_record_ids": unresolved_record_ids,
                    "repaired_findings": remediation_repaired if fixed_ids else 0,
                },
            },
        )
        reconciled += 1

    return {"reconciled": reconciled, "repaired_findings": repaired}


_store: DataIntegrityStore | None = None


def get_data_integrity_store() -> DataIntegrityStore:
    global _store
    if _store is None:
        _store = DataIntegrityStore()
    return _store


def close_data_integrity_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None


def build_integrity_forensic_report(slice_id: str, *, limit: int = 20) -> dict[str, Any]:
    store = get_data_integrity_store()
    slices = {item["slice_id"]: item for item in store.list_slices()}
    slice_info = slices.get(slice_id)
    findings = store.list_findings(slice_id=slice_id, limit=max(limit, 200))
    remediations = store.list_remediations(slice_id=slice_id, limit=20)
    rules = store.list_rules(slice_id=slice_id, active_only=True, limit=20)

    suspicion_counts = Counter(item.get("suspicion_type", "") for item in findings)
    status_counts = Counter(item.get("status", "") for item in findings)
    top_examples = [
        {
            "finding_id": item.get("finding_id"),
            "record_id": item.get("record_id"),
            "suspicion_type": item.get("suspicion_type"),
            "status": item.get("status"),
            "details": item.get("details", {}),
        }
        for item in findings[:limit]
    ]
    recent_remediations = [
        {
            "remediation_id": item.get("remediation_id"),
            "action_type": item.get("action_type"),
            "status": item.get("status"),
            "finished_at": item.get("finished_at"),
            "details": item.get("details", {}),
        }
        for item in remediations[:5]
    ]
    next_actions: list[str] = []
    if slice_info and slice_info.get("status") == "degraded":
        next_actions.append("Inspect the dominant suspicion types before repeating automated remediation.")
    if recent_remediations:
        latest = recent_remediations[0]
        if latest.get("status") == "done":
            next_actions.append(
                f"Compare current suspect records against the last completed remediation '{latest.get('action_type')}'."
            )
    if suspicion_counts:
        next_actions.append(
            "Promote confirmed suspect records to quarantine_candidate only after verifying they represent real schema/payload drift."
        )
    for rule in rules:
        action_hint = (rule.get("guidance") or {}).get("action_hint")
        if action_hint and action_hint not in next_actions:
            next_actions.append(str(action_hint))

    return {
        "slice_id": slice_id,
        "slice": slice_info,
        "summary": {
            "total_findings": len(findings),
            "suspicion_types": dict(suspicion_counts),
            "statuses": dict(status_counts),
        },
        "recent_remediations": recent_remediations,
        "rules": rules,
        "examples": top_examples,
        "next_actions": next_actions,
    }


def build_auto_remediation_guard(slice_id: str, *, cooldown_seconds: float = 3600.0) -> dict[str, Any]:
    store = get_data_integrity_store()
    slice_info = store.get_slice(slice_id)
    active_findings = [
        item
        for item in store.list_findings(slice_id=slice_id, limit=5000)
        if item.get("status") in {"suspect", "quarantine_candidate", "quarantined"}
    ]
    is_degraded = bool(slice_info and slice_info.get("status") == "degraded")
    if not is_degraded and not active_findings:
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "slice_not_actionable",
            "cooldown_seconds": float(cooldown_seconds),
        }

    remediations = store.list_remediations(slice_id=slice_id, limit=20)
    active = [item for item in remediations if item.get("status") in {"queued", "running"}]
    if active:
        latest_active = max(
            active,
            key=lambda item: item.get("started_at") or item.get("created_at") or 0.0,
        )
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "active_remediation_exists",
            "cooldown_seconds": float(cooldown_seconds),
            "active_remediation_id": latest_active.get("remediation_id"),
            "active_action_type": latest_active.get("action_type"),
            "active_status": latest_active.get("status"),
        }

    latest = remediations[0] if remediations else None
    if latest is None:
        return {
            "slice_id": slice_id,
            "allowed": True,
            "reason": "no_recent_remediation",
            "cooldown_seconds": float(cooldown_seconds),
            "slice_status": slice_info.get("status") if slice_info else None,
            "active_findings": len(active_findings),
        }

    last_attempt_at = (
        latest.get("finished_at")
        or latest.get("started_at")
        or latest.get("created_at")
        or 0.0
    )
    age_seconds = max(0.0, time.time() - float(last_attempt_at or 0.0))
    if age_seconds < float(cooldown_seconds):
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "cooldown_active",
            "cooldown_seconds": float(cooldown_seconds),
            "remaining_seconds": max(0.0, float(cooldown_seconds) - age_seconds),
            "latest_remediation_id": latest.get("remediation_id"),
            "latest_action_type": latest.get("action_type"),
            "latest_status": latest.get("status"),
            "slice_status": slice_info.get("status") if slice_info else None,
            "active_findings": len(active_findings),
        }

    return {
        "slice_id": slice_id,
        "allowed": True,
        "reason": "cooldown_elapsed",
        "cooldown_seconds": float(cooldown_seconds),
        "latest_remediation_id": latest.get("remediation_id"),
        "latest_action_type": latest.get("action_type"),
        "latest_status": latest.get("status"),
        "last_attempt_age_seconds": age_seconds,
        "slice_status": slice_info.get("status") if slice_info else None,
        "active_findings": len(active_findings),
    }


def build_auto_discovery_guard(slice_id: str, *, cooldown_seconds: float = 3600.0) -> dict[str, Any]:
    store = get_data_integrity_store()
    if slice_id not in DISCOVERY_DETECTORS:
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "no_discovery_detector",
            "cooldown_seconds": float(cooldown_seconds),
        }
    slice_info = store.get_slice(slice_id)
    if not slice_info or slice_info.get("status") != "degraded":
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "slice_not_degraded",
            "cooldown_seconds": float(cooldown_seconds),
        }
    active_findings = [
        item
        for item in store.list_findings(slice_id=slice_id, limit=5000)
        if item.get("status") in {"suspect", "quarantine_candidate", "quarantined"}
    ]
    if active_findings:
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "active_findings_exist",
            "cooldown_seconds": float(cooldown_seconds),
            "active_findings": len(active_findings),
        }
    details = slice_info.get("details") or {}
    last_checked_at = float(details.get("auto_discovery_checked_at") or 0.0)
    if last_checked_at > 0:
        age_seconds = max(0.0, time.time() - last_checked_at)
        if age_seconds < float(cooldown_seconds):
            return {
                "slice_id": slice_id,
                "allowed": False,
                "reason": "cooldown_active",
                "cooldown_seconds": float(cooldown_seconds),
                "remaining_seconds": max(0.0, float(cooldown_seconds) - age_seconds),
                "last_discovered_count": int(details.get("auto_discovery_discovered") or 0),
            }
        return {
            "slice_id": slice_id,
            "allowed": True,
            "reason": "cooldown_elapsed",
            "cooldown_seconds": float(cooldown_seconds),
            "last_discovered_count": int(details.get("auto_discovery_discovered") or 0),
            "last_discovery_age_seconds": age_seconds,
        }
    return {
        "slice_id": slice_id,
        "allowed": True,
        "reason": "no_recent_auto_discovery",
        "cooldown_seconds": float(cooldown_seconds),
    }


async def maybe_auto_discover_slice(
    slice_id: str,
    *,
    limit: int = 50,
    cooldown_seconds: float = 3600.0,
) -> dict[str, Any]:
    store = get_data_integrity_store()
    guard = build_auto_discovery_guard(slice_id, cooldown_seconds=cooldown_seconds)
    if not guard.get("allowed"):
        return {
            "slice_id": slice_id,
            "performed": False,
            "discovered": 0,
            "guard": guard,
        }
    checked_at = time.time()
    try:
        result = await discover_suspect_records(slice_id, limit=limit)
        discovered = int(result.get("discovered") or 0)
        store.patch_slice_details(
            slice_id=slice_id,
            patch={
                "auto_discovery_checked_at": checked_at,
                "auto_discovery_discovered": discovered,
                "auto_discovery_last_error": "",
            },
        )
        return {
            "slice_id": slice_id,
            "performed": True,
            "discovered": discovered,
            "guard": guard,
        }
    except Exception as exc:
        store.patch_slice_details(
            slice_id=slice_id,
            patch={
                "auto_discovery_checked_at": checked_at,
                "auto_discovery_discovered": 0,
                "auto_discovery_last_error": str(exc),
            },
        )
        raise


def build_integrity_repair_plan(slice_id: str, *, limit: int = 20) -> dict[str, Any]:
    store = get_data_integrity_store()
    forensic = build_integrity_forensic_report(slice_id, limit=limit)
    findings = store.list_findings(slice_id=slice_id, limit=max(limit * 10, 200))
    active_findings = [
        item for item in findings
        if item.get("status") in {"suspect", "quarantine_candidate", "quarantined"}
    ]

    grouped: dict[str, dict[str, Any]] = {}
    for item in active_findings:
        details = item.get("details") or {}
        action_type = str(details.get("suggested_repair") or "manual_review")
        bucket = grouped.setdefault(
            action_type,
            {
                "action_type": action_type,
                "finding_count": 0,
                "record_ids": [],
                "suspicion_types": Counter(),
                "statuses": Counter(),
                "reasons": Counter(),
                "sample_findings": [],
            },
        )
        bucket["finding_count"] += 1
        record_id = str(item.get("record_id") or "")
        if record_id and record_id not in bucket["record_ids"] and len(bucket["record_ids"]) < limit:
            bucket["record_ids"].append(record_id)
        suspicion_type = str(item.get("suspicion_type") or "")
        if suspicion_type:
            bucket["suspicion_types"][suspicion_type] += 1
        status = str(item.get("status") or "")
        if status:
            bucket["statuses"][status] += 1
        reason = str(details.get("reason") or "")
        if reason:
            bucket["reasons"][reason] += 1
        if len(bucket["sample_findings"]) < min(limit, 5):
            bucket["sample_findings"].append(
                {
                    "finding_id": item.get("finding_id"),
                    "record_id": item.get("record_id"),
                    "suspicion_type": item.get("suspicion_type"),
                    "status": item.get("status"),
                }
            )

    actions: list[dict[str, Any]] = []
    for bucket in grouped.values():
        actions.append(
            {
                "action_type": bucket["action_type"],
                "finding_count": bucket["finding_count"],
                "record_ids": bucket["record_ids"],
                "suspicion_types": dict(bucket["suspicion_types"]),
                "statuses": dict(bucket["statuses"]),
                "reasons": dict(bucket["reasons"]),
                "sample_findings": bucket["sample_findings"],
            }
        )
    actions.sort(key=lambda item: (-int(item["finding_count"]), str(item["action_type"])))

    recommended_sequence: list[str] = []
    if forensic.get("slice", {}).get("status") == "degraded":
        recommended_sequence.append("review_forensics")
    if forensic.get("rules"):
        recommended_sequence.append("apply_active_rules")
    if actions:
        recommended_sequence.append("select_targeted_repair_action")
    if any(item["action_type"] == "manual_review" for item in actions):
        recommended_sequence.append("promote_confirmed_records_to_quarantine_candidate")
    if any(item["action_type"] != "manual_review" for item in actions):
        recommended_sequence.append("prepare_targeted_repair_batch")
    recommended_sequence.append("re-run_forensics_after_repair")

    return {
        "slice_id": slice_id,
        "status": forensic.get("slice", {}).get("status", "unknown"),
        "summary": {
            "active_findings": len(active_findings),
            "recommended_sequence": recommended_sequence,
        },
        "forensics": {
            "summary": forensic.get("summary", {}),
            "rules": forensic.get("rules", []),
            "next_actions": forensic.get("next_actions", []),
        },
        "actions": actions,
    }


def build_targeted_repair_batch_preview(
    slice_id: str,
    *,
    action_type: str,
    limit: int = 20,
) -> dict[str, Any]:
    plan = build_integrity_repair_plan(slice_id, limit=limit)
    action = next((item for item in plan.get("actions", []) if item.get("action_type") == action_type), None)
    executor = TARGETED_REPAIR_EXECUTORS.get(action_type)
    supported = bool(action and executor)
    preview = {
        "slice_id": slice_id,
        "action_type": action_type,
        "supported": supported,
        "executor": executor or {},
        "finding_count": 0,
        "record_ids": [],
        "sample_findings": [],
        "reason": "",
    }
    if not action:
        preview["reason"] = "No active findings currently map to this repair action."
        return preview
    if not executor:
        preview["finding_count"] = action.get("finding_count", 0)
        preview["record_ids"] = action.get("record_ids", [])
        preview["sample_findings"] = action.get("sample_findings", [])
        preview["reason"] = "This repair action has no registered executor yet."
        return preview
    preview["finding_count"] = action.get("finding_count", 0)
    preview["record_ids"] = action.get("record_ids", [])
    preview["sample_findings"] = action.get("sample_findings", [])
    return preview


async def queue_targeted_repair_batch(
    *,
    slice_id: str,
    action_type: str,
    requested_by: str,
    queue,
    limit: int = 20,
) -> dict[str, Any]:
    store = get_data_integrity_store()
    preview = build_targeted_repair_batch_preview(slice_id, action_type=action_type, limit=limit)
    if not preview.get("supported"):
        raise ValueError(preview.get("reason") or f"No targeted executor is registered for action {action_type}")
    executor = preview["executor"]
    payload: dict[str, Any] = {"limit": limit}
    if executor.get("supports_record_ids"):
        payload["record_ids"] = list(preview.get("record_ids") or [])
    job_id = await queue.submit(str(executor["job_type"]), payload)
    remediation_id = str(uuid4())
    return store.queue_remediation(
        remediation_id=remediation_id,
        slice_id=slice_id,
        action_type=action_type,
        requested_by=requested_by,
        job_id=job_id,
        details={
            "description": executor.get("description", ""),
            "payload": payload,
            "source": "targeted_repair_batch",
            "finding_count": preview.get("finding_count", 0),
        },
    )


def build_integrity_remediation_outcome(remediation_id: str) -> dict[str, Any]:
    store = get_data_integrity_store()
    remediation = store.get_remediation(remediation_id)
    if remediation is None:
        raise ValueError(f"Remediation {remediation_id} not found")
    details = remediation.get("details") or {}
    closure = details.get("closure_summary") or {}
    targeted_record_ids = [str(item) for item in (closure.get("targeted_record_ids") or []) if item]
    fixed_ids = [str(item) for item in (closure.get("fixed_ids") or []) if item]
    unresolved_record_ids = [str(item) for item in (closure.get("unresolved_record_ids") or []) if item]
    attempted = targeted_record_ids or fixed_ids
    summary = {
        "attempted_record_count": len(attempted),
        "fixed_record_count": len(fixed_ids),
        "unresolved_record_count": len(unresolved_record_ids),
        "repaired_findings": int(closure.get("repaired_findings") or 0),
    }
    next_actions: list[str] = []
    if remediation.get("status") in {"queued", "running"}:
        next_actions.append("Wait for the remediation job to finish before trusting closure results.")
    elif unresolved_record_ids:
        next_actions.append("Review unresolved record ids in the forensic report before repeating automated repair.")
    else:
        next_actions.append("Re-run forensics if you need a fresh slice-level health check after this remediation.")
    return {
        "remediation_id": remediation_id,
        "slice_id": remediation.get("slice_id"),
        "action_type": remediation.get("action_type"),
        "status": remediation.get("status"),
        "requested_by": remediation.get("requested_by"),
        "job_id": remediation.get("job_id"),
        "summary": summary,
        "targeted_record_ids": targeted_record_ids,
        "fixed_ids": fixed_ids,
        "unresolved_record_ids": unresolved_record_ids,
        "details": details,
        "next_actions": next_actions,
    }
