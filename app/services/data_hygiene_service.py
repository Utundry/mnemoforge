from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import hashlib
from pathlib import Path
from threading import Lock
from typing import Any

from app.config import settings
from app.services.learning_store import get_learning_store
from app.services.system_data_root import data_path

logger = logging.getLogger(__name__)

_DB_PATH = data_path("data_hygiene.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hygiene_audits (
    audit_id            TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'ok',
    started_at          REAL NOT NULL,
    finished_at         REAL NOT NULL,
    total_records       INTEGER NOT NULL DEFAULT 0,
    classified_json     TEXT NOT NULL DEFAULT '{}',
    actions_json        TEXT NOT NULL DEFAULT '{}',
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS hygiene_findings (
    finding_id             TEXT PRIMARY KEY,
    store_name             TEXT NOT NULL,
    record_locator         TEXT NOT NULL,
    dataset_class          TEXT NOT NULL,
    recommended_action     TEXT NOT NULL,
    exclude_from_learning  INTEGER NOT NULL DEFAULT 0,
    confidence             REAL NOT NULL DEFAULT 0.0,
    status                 TEXT NOT NULL DEFAULT 'open',
    first_seen_at          REAL NOT NULL,
    last_seen_at           REAL NOT NULL,
    reasons_json           TEXT NOT NULL DEFAULT '[]',
    details_json           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_hygiene_findings_store ON hygiene_findings(store_name, status);
CREATE INDEX IF NOT EXISTS idx_hygiene_findings_class ON hygiene_findings(dataset_class, recommended_action);
CREATE TABLE IF NOT EXISTS hygiene_remediations (
    remediation_id      TEXT PRIMARY KEY,
    recommended_action  TEXT NOT NULL,
    store_name          TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'queued',
    requested_by        TEXT NOT NULL DEFAULT '',
    job_id              TEXT,
    created_at          REAL NOT NULL,
    started_at          REAL,
    finished_at         REAL,
    last_error          TEXT,
    details_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_hygiene_rem_status ON hygiene_remediations(status, created_at);
CREATE TABLE IF NOT EXISTS hygiene_scan_state (
    state_key         TEXT PRIMARY KEY,
    state_json        TEXT NOT NULL DEFAULT '{}',
    updated_at        REAL NOT NULL
);
"""

_SYNTHETIC_TAGS = {
    "test", "tests", "synthetic", "demo", "fixture", "sample", "mock", "fake",
    "pytest", "unittest", "integration-test", "e2e", "benchmark",
}
_AUTO_TEST_CLEANUP_MARKERS = {
    "synthetic",
    "demo",
    "fixture",
    "mock",
    "fake",
    "pytest",
    "unittest",
    "integration-test",
    "e2e",
    "benchmark",
}
_PROJECTION_CATEGORIES = {"doc_section"}
_SERVICE_CATEGORIES = {"coordination_message"}
_CANONICAL_CATEGORIES = {"law", "task", "task_memoir", "improvement", "runtime_hint", "canonical"}
_EVOLUTIONARY_CATEGORIES = {
    "task_change",
    "project_update",
    "project_progress",
    "project-progress",
    "product_progress",
    "project_tasks",
    "task-card",
    "project",
    "project_architecture",
    "project_decision",
    "ux_decision",
    "architecture",
}
_WORKING_KNOWLEDGE_CATEGORIES = {"context", "config", "setting", "skill", "code_component", "general", "qa", "reference"}
_CACHE_OR_OPERATIONAL_CATEGORIES = {"cache", "docs_cache", "projection_cache", "memoir_cache", "route_cache", "index_snapshot"}
_TELEMETRY_EVENT_TYPES = {"tool_call", "tool_result", "memory_write", "llm_mirror", "episode_start", "episode_end"}
_SERVICE_EVENT_TYPES = {"artifact_promoted", "artifact_suggested", "candidate_approved", "candidate_rejected", "improvement_created"}
_LEARNING_EVENT_TYPES = {
    "dialogue_excerpt", "dialogue_signal", "user_request", "user_feedback",
    "memory_use", "outcome_recorded", "session_outcome", "artifact_feedback",
}
HYGIENE_REMEDIATION_REGISTRY: dict[str, dict[str, Any]] = {
    "exclude-from-learning": {
        "job_type": "data_hygiene_apply_exclusion",
        "description": "Mark records as excluded from learning or acknowledge rule-based exclusion for service traces.",
    },
    "archive": {
        "job_type": "data_hygiene_mark_archive",
        "description": "Mark derived or service records as archived without destructive deletion.",
    },
    "delete-reviewed": {
        "job_type": "data_hygiene_reviewed_delete",
        "description": "Delete reviewed synthetic/test traces only after explicit manual promotion.",
    },
    "delete-approved": {
        "job_type": "data_hygiene_approved_delete",
        "description": "Delete explicitly quarantined live records only after dry-run and explicit approval.",
    },
}
DATASET_POLICY_REGISTRY: dict[str, dict[str, Any]] = {
    "canonical_knowledge": {
        "retention": "keep",
        "learning_mode": "allowed",
        "auto_remediate": False,
        "manual_review_required": False,
        "description": "Governed project knowledge remains in the main knowledge substrate.",
    },
    "evolutionary_knowledge": {
        "retention": "keep",
        "learning_mode": "allowed",
        "auto_remediate": False,
        "manual_review_required": False,
        "description": "Project-evolution records (task changes, progress, architecture drift) remain in the knowledge substrate.",
    },
    "working_knowledge": {
        "retention": "keep",
        "learning_mode": "allowed",
        "auto_remediate": False,
        "manual_review_required": False,
        "description": "Operational project context may stay in the substrate and participate in learning.",
    },
    "telemetry_trace": {
        "retention": "exclude-from-learning",
        "learning_mode": "blocked",
        "auto_remediate": True,
        "manual_review_required": False,
        "description": "Telemetry and tool traces should not feed learning loops.",
    },
    "service_operational": {
        "retention": "archive",
        "learning_mode": "blocked",
        "auto_remediate": True,
        "manual_review_required": False,
        "description": "Service artifacts and operational traces should be archived out of hot knowledge paths.",
    },
    "temporary_projection": {
        "retention": "archive",
        "learning_mode": "blocked",
        "auto_remediate": True,
        "manual_review_required": False,
        "description": "Derived projections should be archived rather than treated as source knowledge.",
    },
    "raw_dialogue_trace": {
        "retention": "exclude-from-learning",
        "learning_mode": "blocked",
        "auto_remediate": True,
        "manual_review_required": False,
        "description": "Raw conversation traces should not become direct learning substrate without extraction.",
    },
    "synthetic_test": {
        "retention": "delete",
        "learning_mode": "blocked",
        "auto_remediate": False,
        "manual_review_required": True,
        "description": "Synthetic and test data should leave live stores, but only after explicit review.",
    },
    "stale_guidance": {
        "retention": "exclude-from-learning",
        "learning_mode": "blocked",
        "auto_remediate": True,
        "manual_review_required": False,
        "description": "Outdated operational guidance should not feed agent learning or hot retrieval.",
    },
    "governance_duplicate": {
        "retention": "review",
        "learning_mode": "blocked",
        "auto_remediate": False,
        "manual_review_required": True,
        "description": "Potential duplicate or superseded governance artifacts require operator review; never merge or delete silently.",
    },
}
_GOVERNANCE_PROMOTED_SCOPES = {"family", "domain", "principle", "meta"}


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _has_synthetic_markers(*values: Any) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                text = str(item).strip().lower()
                if text in _SYNTHETIC_TAGS:
                    reasons.append(f"synthetic_marker:{text}")
        else:
            text = str(value).strip().lower()
            if not text:
                continue
            if text in _SYNTHETIC_TAGS:
                reasons.append(f"synthetic_marker:{text}")
            elif any(token in text for token in ("pytest", "synthetic", "fixture", "mock", "demo dataset", "fake data")):
                reasons.append("synthetic_text_marker")
    return bool(reasons), reasons


def _known_public_tools() -> set[str]:
    try:
        from app.services.mcp_workflow_specs import load_tool_contract_catalog_spec

        catalog = load_tool_contract_catalog_spec("public_surface")
        return {str(tool.name or "").strip() for tool in catalog.tools if str(tool.name or "").strip()}
    except Exception:
        return {"help", "state", "get", "submit", "put"}


def _known_mailbox_forms() -> set[str]:
    try:
        from app.services.mcp_workflow_specs import list_mailbox_form_specs

        return {str(form.id or "").strip() for form in list_mailbox_form_specs() if str(form.id or "").strip()}
    except Exception:
        return set()


def _governed_payload_class(category: str, source: str, tags: list[str]) -> str:
    if category in _CANONICAL_CATEGORIES or source in {"improvement", "improvement_created"}:
        return "canonical_knowledge"
    if any(tag in {"entity:task", "entity:task_change", "entity:improvement", "entity:law"} for tag in tags):
        return "evolutionary_knowledge"
    if category in _EVOLUTIONARY_CATEGORIES or category.startswith("project_"):
        return "evolutionary_knowledge"
    return ""


def _stale_guidance_markers(payload: dict[str, Any]) -> list[str]:
    content = str(payload.get("content") or "")
    category = str(payload.get("category") or "").strip().lower()
    source = str(payload.get("source") or "").strip().lower()
    tags = [str(tag).strip().lower() for tag in (payload.get("tags") or [])]
    haystack = " ".join([content, category, source, " ".join(tags)]).lower()
    if not haystack.strip():
        return []
    if _governed_payload_class(category, source, tags):
        return []

    reasons: list[str] = []
    obsolete_tool_replacements = {
        "memory_store": "submit:store_memory",
        "record_work_result": "submit:record_progress",
        "approve_checkpoint_draft": "submit:record_progress",
        "reject_checkpoint_draft": "submit:record_progress",
    }
    current_tools = _known_public_tools()
    for tool_name, replacement in obsolete_tool_replacements.items():
        if tool_name in current_tools:
            continue
        if not re.search(rf"\b{re.escape(tool_name)}\b", haystack):
            continue
        commandish = re.search(
            rf"\b(use|call|invoke|tool|route|mcp|api|submit|record|store|write|create)\b[^.\n]{{0,120}}\b{re.escape(tool_name)}\b",
            haystack,
        ) or re.search(
            rf"\b{re.escape(tool_name)}\b[^.\n]{{0,120}}\b(use|call|invoke|tool|route|mcp|api|submit|record|store|write|create)\b",
            haystack,
        )
        if commandish:
            reasons.append(f"stale_tool_guidance:{tool_name}->replacement:{replacement}")

    known_forms = _known_mailbox_forms()
    form_mentions = set(re.findall(r"\bform[_ -]?id\s*[:=]\s*['\"]?([a-z][a-z0-9_]{2,})", haystack))
    form_mentions.update(re.findall(r"\bsubmit\s*\(\s*['\"]?([a-z][a-z0-9_]{2,})", haystack))
    for form_id in sorted(form_mentions):
        if form_id not in known_forms and form_id not in current_tools:
            reasons.append(f"unknown_mailbox_form_guidance:{form_id}")
    return reasons


def classify_memory_payload(payload: dict[str, Any]) -> dict[str, Any]:
    category = str(payload.get("category") or "").strip().lower()
    source = str(payload.get("source") or "").strip().lower()
    tags = [str(tag).strip().lower() for tag in (payload.get("tags") or [])]
    project = str(payload.get("project") or "").strip().lower()
    content = str(payload.get("content") or "")[:400].lower()
    meta = payload.get("meta") or {}
    reasons: list[str] = []

    synthetic, synthetic_reasons = _has_synthetic_markers(tags, source, project, content, meta.get("origin"), meta.get("environment"))
    if synthetic:
        governed_class = _governed_payload_class(category, source, tags)
        if governed_class:
            reasons.extend(["synthetic_marker_ignored_for_governed_record", *synthetic_reasons])
            if governed_class == "canonical_knowledge":
                return {
                    "dataset_class": "canonical_knowledge",
                    "recommended_action": "keep",
                    "exclude_from_learning": False,
                    "confidence": 0.9,
                    "reasons": reasons,
                }
            return {
                "dataset_class": "evolutionary_knowledge",
                "recommended_action": "keep",
                "exclude_from_learning": False,
                "confidence": 0.82,
                "reasons": reasons,
            }
        reasons.extend(synthetic_reasons)
        return {
            "dataset_class": "synthetic_test",
            "recommended_action": "delete",
            "exclude_from_learning": True,
            "confidence": 0.95,
            "reasons": reasons,
        }

    if category in _SERVICE_CATEGORIES or any(tag.startswith("entity:coordination_message") for tag in tags):
        reasons.append("coordination_service_record")
        return {
            "dataset_class": "service_operational",
            "recommended_action": "archive",
            "exclude_from_learning": True,
            "confidence": 0.9,
            "reasons": reasons,
        }

    if category in _CACHE_OR_OPERATIONAL_CATEGORIES or "cache" in source or any(tag == "cache" or tag.startswith("cache:") for tag in tags):
        reasons.append("cache_or_operational_record")
        return {
            "dataset_class": "service_operational",
            "recommended_action": "archive",
            "exclude_from_learning": True,
            "confidence": 0.8,
            "reasons": reasons,
        }

    stale_guidance_reasons = _stale_guidance_markers(payload)
    if stale_guidance_reasons:
        reasons.extend(stale_guidance_reasons)
        return {
            "dataset_class": "stale_guidance",
            "recommended_action": "exclude-from-learning",
            "exclude_from_learning": True,
            "confidence": 0.88,
            "reasons": reasons,
        }

    if category in _PROJECTION_CATEGORIES or "projection" in source or any("projection" in tag for tag in tags):
        reasons.append("derived_projection_record")
        return {
            "dataset_class": "temporary_projection",
            "recommended_action": "archive",
            "exclude_from_learning": True,
            "confidence": 0.85,
            "reasons": reasons,
        }

    if category in _CANONICAL_CATEGORIES:
        reasons.append("governed_project_knowledge")
        return {
            "dataset_class": "canonical_knowledge",
            "recommended_action": "keep",
            "exclude_from_learning": False,
            "confidence": 0.9,
            "reasons": reasons,
        }

    if category == "conversation" or source == "conversation":
        reasons.append("raw_dialogue_trace")
        return {
            "dataset_class": "raw_dialogue_trace",
            "recommended_action": "exclude-from-learning",
            "exclude_from_learning": True,
            "confidence": 0.8,
            "reasons": reasons,
        }

    if source in {"improvement", "improvement_created"}:
        reasons.append("improvement_source_record")
        return {
            "dataset_class": "canonical_knowledge",
            "recommended_action": "keep",
            "exclude_from_learning": False,
            "confidence": 0.75,
            "reasons": reasons,
        }

    if (
        category in _EVOLUTIONARY_CATEGORIES
        or category.startswith("project_")
        or any(tag.startswith("project:") for tag in tags)
        or any(tag in {"entity:task", "entity:task_change"} for tag in tags)
    ):
        reasons.append("project_evolution_record")
        return {
            "dataset_class": "evolutionary_knowledge",
            "recommended_action": "keep",
            "exclude_from_learning": False,
            "confidence": 0.75,
            "reasons": reasons,
        }

    if category in _WORKING_KNOWLEDGE_CATEGORIES or source.startswith(("client-scan:", "watcher:")):
        reasons.append("working_project_knowledge")
        return {
            "dataset_class": "working_knowledge",
            "recommended_action": "keep",
            "exclude_from_learning": False,
            "confidence": 0.7,
            "reasons": reasons,
        }

    reasons.append("unclassified_memory_record")
    return {
        "dataset_class": "service_operational",
        "recommended_action": "exclude-from-learning",
        "exclude_from_learning": True,
        "confidence": 0.55,
        "reasons": reasons,
    }


def classify_learning_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "").strip()
    context_signature = str(event.get("context_signature") or "").lower()
    project = str(event.get("project") or "").lower()
    payload = event.get("payload")
    if payload is None:
        payload = _loads_json(event.get("payload_json"), {})
    reasons: list[str] = []

    synthetic, synthetic_reasons = _has_synthetic_markers(
        event.get("agent_id"),
        event.get("transport"),
        project,
        context_signature,
        payload.get("transport") if isinstance(payload, dict) else None,
        payload.get("source_path") if isinstance(payload, dict) else None,
    )
    if synthetic:
        reasons.extend(synthetic_reasons)
        return {
            "dataset_class": "synthetic_test",
            "recommended_action": "delete",
            "exclude_from_learning": True,
            "confidence": 0.95,
            "reasons": reasons,
        }

    if event_type in _TELEMETRY_EVENT_TYPES:
        reasons.append(f"telemetry_event:{event_type}")
        return {
            "dataset_class": "telemetry_trace",
            "recommended_action": "exclude-from-learning",
            "exclude_from_learning": True,
            "confidence": 0.9,
            "reasons": reasons,
        }

    if event_type in _SERVICE_EVENT_TYPES:
        reasons.append(f"service_event:{event_type}")
        return {
            "dataset_class": "service_operational",
            "recommended_action": "archive",
            "exclude_from_learning": True,
            "confidence": 0.8,
            "reasons": reasons,
        }

    if event_type in _LEARNING_EVENT_TYPES:
        reasons.append(f"learning_signal:{event_type}")
        return {
            "dataset_class": "working_knowledge",
            "recommended_action": "keep",
            "exclude_from_learning": False,
            "confidence": 0.8,
            "reasons": reasons,
        }

    reasons.append(f"unknown_event_type:{event_type or 'unknown'}")
    return {
        "dataset_class": "service_operational",
        "recommended_action": "exclude-from-learning",
        "exclude_from_learning": True,
        "confidence": 0.6,
        "reasons": reasons,
    }


def memory_payload_should_be_excluded_from_learning(payload: dict[str, Any]) -> bool:
    return bool(classify_memory_payload(payload).get("exclude_from_learning"))


def learning_event_should_be_excluded_from_learning(event: dict[str, Any]) -> bool:
    return bool(classify_learning_event(event).get("exclude_from_learning"))


def policy_for_dataset_class(dataset_class: str) -> dict[str, Any]:
    return dict(DATASET_POLICY_REGISTRY.get(dataset_class, {
        "retention": "exclude-from-learning",
        "learning_mode": "blocked",
        "auto_remediate": False,
        "manual_review_required": True,
        "description": "Unknown dataset class defaults to blocked learning and manual review.",
    }))


def _unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _extract_hygiene_project_scopes(item: dict[str, Any]) -> list[str]:
    details = item.get("details") or {}
    candidates: list[str] = []
    for key in (
        "project",
        "project_id",
        "canonical_project",
        "source_project",
        "target_project",
    ):
        value = details.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(str(entry) for entry in value)
    for tag in details.get("tags") or []:
        text = str(tag or "").strip()
        if text.startswith("project:"):
            candidates.append(text.split(":", 1)[1])
    for reason in item.get("reasons") or []:
        text = str(reason or "").strip()
        if text.startswith("project:"):
            candidates.append(text.split(":", 1)[1])
    return _unique_nonempty(candidates)


def classify_hygiene_finding_scope(
    item: dict[str, Any],
    *,
    current_project: str | None = None,
) -> dict[str, Any]:
    projects = _extract_hygiene_project_scopes(item)
    current = str(current_project or "").strip()
    if len(projects) > 1:
        scope_kind = "multi_project"
        scope_project = ""
    elif projects:
        scope_kind = "project"
        scope_project = projects[0]
    else:
        scope_kind = "system_or_unknown"
        scope_project = ""

    if not current:
        relation = "unspecified_current_project"
    elif scope_kind == "project" and scope_project == current:
        relation = "current_project"
    elif scope_kind == "project":
        relation = "outside_current_project"
    elif scope_kind == "multi_project" and current in projects:
        relation = "includes_current_project"
    elif scope_kind == "multi_project":
        relation = "outside_current_project"
    else:
        relation = "system_or_unknown_scope"

    warning = ""
    if current and relation == "outside_current_project":
        target = ", ".join(projects) if projects else "unknown project"
        warning = f"Hygiene finding targets {target}, not current project {current}."
    elif current and relation == "system_or_unknown_scope":
        warning = f"Hygiene finding has system/unknown scope; do not present it as current project {current} work."

    return {
        "scope_kind": scope_kind,
        "scope_project": scope_project,
        "projects": projects,
        "relation_to_current_project": relation,
        "warning": warning,
    }


def build_hygiene_scope_summary(
    findings: list[dict[str, Any]],
    *,
    current_project: str | None = None,
    sample_size: int = 5,
) -> dict[str, Any]:
    by_scope_kind: dict[str, int] = {}
    by_relation: dict[str, int] = {}
    by_project: dict[str, int] = {}
    warnings: list[dict[str, str]] = []
    for item in findings:
        scope = classify_hygiene_finding_scope(item, current_project=current_project)
        scope_kind = str(scope.get("scope_kind") or "system_or_unknown")
        relation = str(scope.get("relation_to_current_project") or "unspecified_current_project")
        by_scope_kind[scope_kind] = by_scope_kind.get(scope_kind, 0) + 1
        by_relation[relation] = by_relation.get(relation, 0) + 1
        for project in scope.get("projects") or []:
            by_project[project] = by_project.get(project, 0) + 1
        warning = str(scope.get("warning") or "")
        if warning and len(warnings) < sample_size:
            warnings.append({
                "finding_id": str(item.get("finding_id") or ""),
                "warning": warning,
            })
    return {
        "current_project": str(current_project or ""),
        "total_findings": len(findings),
        "by_scope_kind": by_scope_kind,
        "by_relation": by_relation,
        "by_project": by_project,
        "warnings": warnings,
    }


def _normalized_governance_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    text = re.sub(r"[^a-z0-9а-яё ]+", "", text)
    return text.strip()


def _is_law_like_payload(payload: dict[str, Any]) -> bool:
    category = str(payload.get("category") or "").strip().lower()
    source = str(payload.get("source") or "").strip().lower()
    tags = [str(tag).strip().lower() for tag in (payload.get("tags") or [])]
    entity_type = str(payload.get("entity_type") or "").strip().lower()
    return bool(
        category == "law"
        or entity_type == "project_law"
        or source == "project-law"
        or "law" in tags
        or "entity:law" in tags
    )


def _governance_record_signature(record: dict[str, Any]) -> str:
    payload = record.get("payload") or record
    statement = (
        payload.get("statement")
        or payload.get("content")
        or payload.get("title")
        or ""
    )
    return _normalized_governance_text(statement)


def detect_governance_duplicate_review_packets(
    records: list[dict[str, Any]],
    *,
    current_project: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        payload = record.get("payload") or record
        if not _is_law_like_payload(payload):
            continue
        signature = _governance_record_signature(record)
        if len(signature) < 24:
            continue
        grouped.setdefault(signature, []).append(record)

    packets: list[dict[str, Any]] = []
    for signature, items in grouped.items():
        if len(items) < 2:
            continue
        projects = _unique_nonempty([
            str((item.get("payload") or item).get("project") or "")
            for item in items
        ])
        scopes = _unique_nonempty([
            str((item.get("payload") or item).get("scope") or "")
            for item in items
        ])
        has_promoted = any(scope in _GOVERNANCE_PROMOTED_SCOPES for scope in scopes)
        has_project_local = any(scope == "project" for scope in scopes)
        suspicion_type = (
            "project_law_covered_by_promoted_law"
            if has_promoted and has_project_local
            else "duplicate_governance_artifact"
        )
        signature_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
        sample_records = []
        for item in items[:10]:
            payload = item.get("payload") or item
            sample_records.append({
                "store_name": str(item.get("store_name") or "qdrant_memories"),
                "record_locator": str(item.get("record_locator") or item.get("id") or ""),
                "title": str(payload.get("title") or "")[:256],
                "project": str(payload.get("project") or ""),
                "scope": str(payload.get("scope") or ""),
                "status": str(payload.get("status") or ""),
            })
        scope_summary = build_hygiene_scope_summary(
            [
                {
                    "finding_id": str(row.get("record_locator") or ""),
                    "details": {
                        "project": str(row.get("project") or ""),
                        "tags": [f"project:{row.get('project')}"] if row.get("project") else [],
                    },
                    "reasons": [],
                }
                for row in sample_records
            ],
            current_project=current_project,
        )
        packets.append({
            "review_packet_id": f"governance-duplicate:{signature_hash}",
            "signature_hash": signature_hash,
            "suspicion_type": suspicion_type,
            "record_count": len(items),
            "projects": projects,
            "scopes": scopes,
            "scope_summary": scope_summary,
            "sample_records": sample_records,
            "recommended_action": "operator_review",
            "safety": {
                "auto_merge_allowed": False,
                "auto_delete_allowed": False,
                "why": "Duplicate governance artifacts can encode authority conflicts; resolve only through reviewed supersede/suppress decisions.",
            },
        })
        if len(packets) >= limit:
            break
    packets.sort(key=lambda item: (-int(item.get("record_count") or 0), str(item.get("suspicion_type") or "")))
    return packets


class DataHygieneStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM hygiene_audits")
            self._conn.execute("DELETE FROM hygiene_findings")
            self._conn.execute("DELETE FROM hygiene_scan_state")
            self._conn.commit()

    def record_audit(
        self,
        *,
        audit_id: str,
        status: str,
        started_at: float,
        finished_at: float,
        total_records: int,
        classified: dict[str, int],
        actions: dict[str, int],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO hygiene_audits(
                    audit_id, status, started_at, finished_at, total_records,
                    classified_json, actions_json, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    status,
                    started_at,
                    finished_at,
                    total_records,
                    json.dumps(classified),
                    json.dumps(actions),
                    json.dumps(details),
                ),
            )
            self._conn.commit()
        return self.latest_audit() or {}

    def latest_audit(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hygiene_audits ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["classified"] = _loads_json(data.pop("classified_json", "{}"), {})
        data["actions"] = _loads_json(data.pop("actions_json", "{}"), {})
        data["details"] = _loads_json(data.pop("details_json", "{}"), {})
        return data

    def get_scan_state(self, state_key: str = "default") -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM hygiene_scan_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        if row is None:
            return {}
        return _loads_json(row["state_json"], {})

    def set_scan_state(self, state: dict[str, Any], *, state_key: str = "default") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO hygiene_scan_state(state_key, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (state_key, json.dumps(state), now),
            )
            self._conn.commit()
        return self.get_scan_state(state_key)

    def upsert_finding(
        self,
        *,
        finding_id: str,
        store_name: str,
        record_locator: str,
        dataset_class: str,
        recommended_action: str,
        exclude_from_learning: bool,
        confidence: float,
        reasons: list[str],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT first_seen_at, status FROM hygiene_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            first_seen_at = existing["first_seen_at"] if existing else now
            preserved_status = existing["status"] if existing else "open"
            self._conn.execute(
                """
                INSERT INTO hygiene_findings(
                    finding_id, store_name, record_locator, dataset_class,
                    recommended_action, exclude_from_learning, confidence, status,
                    first_seen_at, last_seen_at, reasons_json, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(finding_id) DO UPDATE SET
                    store_name=excluded.store_name,
                    record_locator=excluded.record_locator,
                    dataset_class=excluded.dataset_class,
                    recommended_action=excluded.recommended_action,
                    exclude_from_learning=excluded.exclude_from_learning,
                    confidence=excluded.confidence,
                    status=excluded.status,
                    first_seen_at=excluded.first_seen_at,
                    last_seen_at=excluded.last_seen_at,
                    reasons_json=excluded.reasons_json,
                    details_json=excluded.details_json
                """,
                (
                    finding_id,
                    store_name,
                    record_locator,
                    dataset_class,
                    recommended_action,
                    1 if exclude_from_learning else 0,
                    confidence,
                    preserved_status,
                    first_seen_at,
                    now,
                    json.dumps(reasons),
                    json.dumps(details),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM hygiene_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        return self._decode_finding(row)

    def list_findings(
        self,
        *,
        store_name: str | None = None,
        dataset_class: str | None = None,
        recommended_action: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM hygiene_findings WHERE 1=1"
        params: list[Any] = []
        if store_name:
            sql += " AND store_name = ?"
            params.append(store_name)
        if dataset_class:
            sql += " AND dataset_class = ?"
            params.append(dataset_class)
        if recommended_action:
            sql += " AND recommended_action = ?"
            params.append(recommended_action)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY confidence DESC, last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode_finding(row) for row in rows]

    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hygiene_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_finding(row)

    def list_open_findings_for_action(
        self,
        *,
        recommended_action: str,
        store_name: str | None = None,
        dataset_class: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT * FROM hygiene_findings
            WHERE status = 'open' AND recommended_action = ?
        """
        params: list[Any] = [recommended_action]
        if store_name:
            sql += " AND store_name = ?"
            params.append(store_name)
        if dataset_class:
            sql += " AND dataset_class = ?"
            params.append(dataset_class)
        sql += " ORDER BY confidence DESC, last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode_finding(row) for row in rows]

    def set_finding_status(self, *, finding_id: str, status: str) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute(
                "UPDATE hygiene_findings SET status = ?, last_seen_at = ? WHERE finding_id = ?",
                (status, time.time(), finding_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM hygiene_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        return self._decode_finding(row) if row else None

    def resolve_open_findings_for_record(
        self,
        *,
        store_name: str,
        record_locator: str,
        reason: str,
    ) -> int:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT finding_id, reasons_json, details_json
                FROM hygiene_findings
                WHERE store_name = ? AND record_locator = ? AND status = 'open'
                """,
                (store_name, record_locator),
            ).fetchall()
            updated = 0
            for row in rows:
                reasons = _loads_json(row["reasons_json"], [])
                if reason not in reasons:
                    reasons.append(reason)
                details = _loads_json(row["details_json"], {})
                details["resolved_by"] = reason
                self._conn.execute(
                    """
                    UPDATE hygiene_findings
                    SET status = 'resolved', last_seen_at = ?, reasons_json = ?, details_json = ?
                    WHERE finding_id = ?
                    """,
                    (now, json.dumps(reasons), json.dumps(details), row["finding_id"]),
                )
                updated += 1
            self._conn.commit()
        return updated

    def list_remediations(
        self,
        *,
        recommended_action: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM hygiene_remediations WHERE 1=1"
        params: list[Any] = []
        if recommended_action:
            sql += " AND recommended_action = ?"
            params.append(recommended_action)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode_remediation(row) for row in rows]

    def queue_remediation(
        self,
        *,
        remediation_id: str,
        recommended_action: str,
        store_name: str,
        requested_by: str,
        job_id: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT * FROM hygiene_remediations
                WHERE recommended_action = ? AND store_name = ? AND status IN ('queued','running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (recommended_action, store_name),
            ).fetchone()
            if existing:
                return self._decode_remediation(existing)
            self._conn.execute(
                """
                INSERT INTO hygiene_remediations(
                    remediation_id, recommended_action, store_name, status, requested_by,
                    job_id, created_at, details_json
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    remediation_id,
                    recommended_action,
                    store_name,
                    requested_by,
                    job_id,
                    now,
                    json.dumps(details),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM hygiene_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
        return self._decode_remediation(row)

    def sync_remediations_from_jobs(self, jobs: list[dict[str, Any]]) -> int:
        changed = 0
        by_job_id = {job["id"]: job for job in jobs}
        active = self.list_remediations(limit=500)
        for item in active:
            if item.get("status") in {"done", "failed"}:
                continue
            job = by_job_id.get(item.get("job_id"))
            if not job:
                continue
            target = None
            if job.get("status") == "running" and item.get("status") == "queued":
                target = "running"
            elif job.get("status") == "done" and item.get("status") != "done":
                target = "done"
            elif job.get("status") == "failed" and item.get("status") != "failed":
                target = "failed"
            if target:
                self.sync_remediation_status(
                    remediation_id=item["remediation_id"],
                    status=target,
                    error=str(job.get("error") or ""),
                )
                changed += 1
        return changed

    def sync_remediation_status(self, *, remediation_id: str, status: str, error: str = "") -> None:
        now = time.time()
        with self._lock:
            if status == "running":
                self._conn.execute(
                    """
                    UPDATE hygiene_remediations
                    SET status = ?, started_at = COALESCE(started_at, ?), last_error = NULL
                    WHERE remediation_id = ?
                    """,
                    (status, now, remediation_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE hygiene_remediations
                    SET status = ?, finished_at = ?, last_error = ?
                    WHERE remediation_id = ?
                    """,
                    (status, now, error or None, remediation_id),
                )
            self._conn.commit()

    def patch_remediation_details(self, *, remediation_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT details_json FROM hygiene_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
            if row is None:
                return None
            details = _loads_json(row["details_json"], {})
            details.update(patch)
            self._conn.execute(
                "UPDATE hygiene_remediations SET details_json = ? WHERE remediation_id = ?",
                (json.dumps(details), remediation_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM hygiene_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
        return self._decode_remediation(updated) if updated else None

    def patch_finding_details(
        self,
        *,
        finding_id: str,
        patch: dict[str, Any],
        append_reasons: list[str] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT details_json, reasons_json FROM hygiene_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            if row is None:
                return None
            details = _loads_json(row["details_json"], {})
            details.update(patch)
            reasons = _loads_json(row["reasons_json"], [])
            for reason in append_reasons or []:
                if reason not in reasons:
                    reasons.append(reason)
            self._conn.execute(
                """
                UPDATE hygiene_findings
                SET details_json = ?, reasons_json = ?, last_seen_at = ?
                WHERE finding_id = ?
                """,
                (json.dumps(details), json.dumps(reasons), time.time(), finding_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM hygiene_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        return self._decode_finding(updated) if updated else None

    def overview(self, *, current_project: str | None = None) -> dict[str, Any]:
        latest = self.latest_audit()
        findings = self.list_findings(limit=1000)
        active = [item for item in findings if item.get("status") in {"open", "quarantine_candidate", "quarantined", "manual_review"}]
        remediations = self.list_remediations(limit=200)
        active_remediations = [item for item in remediations if item.get("status") in {"queued", "running"}]
        by_class: dict[str, int] = {}
        by_action: dict[str, int] = {}
        exclude_count = 0
        manual_review_count = 0
        quarantine_count = 0
        for item in active:
            by_class[item["dataset_class"]] = by_class.get(item["dataset_class"], 0) + 1
            by_action[item["recommended_action"]] = by_action.get(item["recommended_action"], 0) + 1
            exclude_count += 1 if item.get("exclude_from_learning") else 0
            if item.get("status") == "manual_review":
                manual_review_count += 1
            if item.get("status") in {"quarantine_candidate", "quarantined"}:
                quarantine_count += 1
        return {
            "status": "warning" if active else "ok",
            "active_findings": len(active),
            "by_class": by_class,
            "by_action": by_action,
            "exclude_from_learning_count": exclude_count,
            "manual_review_count": manual_review_count,
            "quarantine_count": quarantine_count,
            "scope_summary": build_hygiene_scope_summary(active, current_project=current_project),
            "policies": DATASET_POLICY_REGISTRY,
            "recommended_remediations": {
                action: cfg for action, cfg in HYGIENE_REMEDIATION_REGISTRY.items()
            },
            "active_remediations": active_remediations,
            "latest_audit": latest,
        }

    @staticmethod
    def _decode_finding(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        data = dict(row)
        data["exclude_from_learning"] = bool(data.get("exclude_from_learning"))
        data["reasons"] = _loads_json(data.pop("reasons_json", "[]"), [])
        data["details"] = _loads_json(data.pop("details_json", "{}"), {})
        return data

    @staticmethod
    def _decode_remediation(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        data = dict(row)
        data["details"] = _loads_json(data.pop("details_json", "{}"), {})
        return data

    def close(self) -> None:
        with self._lock:
            self._conn.close()


async def run_data_hygiene_audit(
    qdrant,
    *,
    memory_limit: int = 1000,
    event_limit: int = 1000,
    qdrant_offset: Any | None = None,
    event_before_ts: float | None = None,
    event_before_id: int | None = None,
) -> dict[str, Any]:
    import uuid
    from app.services.memory_store import get_memory_store

    store = get_data_hygiene_store()
    started_at = time.time()
    classified: dict[str, int] = {}
    actions: dict[str, int] = {}
    scanned_memories = 0
    scanned_events = 0
    findings_count = 0
    resolved_stale_findings = 0
    memory_scan_source = "qdrant"
    qdrant_scan_error = ""
    governance_records: list[dict[str, Any]] = []

    async def _payload_from_store(memory_id: str) -> dict[str, Any] | None:
        row = await get_memory_store().get(memory_id)
        if not row:
            return None
        metadata = dict(row.get("metadata") or {})
        payload = dict(metadata)
        payload.setdefault("category", metadata.get("category") or row.get("category") or "")
        payload.setdefault("content", row.get("content") or "")
        return payload

    async def _repair_qdrant_payload_from_store(memory_id: str, payload: dict[str, Any]) -> bool:
        try:
            points = await qdrant._client.retrieve(
                collection_name=settings.qdrant_collection_name,
                ids=[memory_id],
                with_payload=False,
                with_vectors=True,
            )
            if not points:
                return False
            vector = getattr(points[0], "vector", None)
            if isinstance(vector, dict):
                vector = next(iter(vector.values()), None)
            if not isinstance(vector, list) or not vector:
                return False
            from qdrant_client.http import models as qmodels

            await qdrant._client.upsert(
                collection_name=settings.qdrant_collection_name,
                points=[qmodels.PointStruct(id=memory_id, vector=vector, payload=payload)],
            )
            logger.warning("Data hygiene audit auto-repaired qdrant payload for %s from SQLite", memory_id)
            return True
        except Exception as exc:
            logger.warning("Data hygiene audit failed to auto-repair qdrant payload for %s: %s", memory_id, exc)
            return False

    async def _retrieve_payloads_resilient(
        point_ids: list[str],
    ) -> tuple[dict[str, dict[str, Any]], list[str], int, int]:
        if not point_ids:
            return {}, [], 0, 0
        try:
            records = await qdrant._client.retrieve(
                collection_name=settings.qdrant_collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=False,
            )
            payloads: dict[str, dict[str, Any]] = {}
            repair_count = 0
            for record in records:
                record_id = str(record.id)
                payload = dict(record.payload or {})
                if payload:
                    payloads[record_id] = payload
                    continue
                fallback_payload = await _payload_from_store(record_id)
                if fallback_payload is None:
                    payloads[record_id] = payload
                    continue
                repaired = await _repair_qdrant_payload_from_store(record_id, fallback_payload)
                logger.warning("Data hygiene audit replaced empty qdrant payload for %s from SQLite", record_id)
                payloads[record_id] = fallback_payload
                if repaired:
                    repair_count += 1
            missing = [point_id for point_id in point_ids if point_id not in payloads]
            return payloads, missing, 0, repair_count
        except Exception as exc:
            if len(point_ids) == 1:
                fallback_payload = await _payload_from_store(point_ids[0])
                if fallback_payload is not None:
                    repaired = await _repair_qdrant_payload_from_store(point_ids[0], fallback_payload)
                    logger.warning(
                        "Data hygiene audit hydrated %s from SQLite after qdrant payload retrieve failure: %s",
                        point_ids[0],
                        exc,
                    )
                    return {point_ids[0]: fallback_payload}, [], 1, (1 if repaired else 0)
                logger.warning("Data hygiene audit skipped qdrant payload hydration for %s: %s", point_ids[0], exc)
                return {}, list(point_ids), 0, 0
            midpoint = max(1, len(point_ids) // 2)
            left_payloads, left_missing, left_fallbacks, left_repairs = await _retrieve_payloads_resilient(point_ids[:midpoint])
            right_payloads, right_missing, right_fallbacks, right_repairs = await _retrieve_payloads_resilient(point_ids[midpoint:])
            merged = dict(left_payloads)
            merged.update(right_payloads)
            return (
                merged,
                left_missing + right_missing,
                left_fallbacks + right_fallbacks,
                left_repairs + right_repairs,
            )

    offset = qdrant_offset
    remaining = max(1, memory_limit)
    skipped_hydration_ids: list[str] = []
    sqlite_hydration_fallbacks = 0
    qdrant_payload_auto_repairs = 0
    qdrant_wrapped = False
    try:
        while remaining > 0:
            batch_size = min(250, remaining)
            points, next_offset = await qdrant._client.scroll(
                collection_name=settings.qdrant_collection_name,
                limit=batch_size,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            if not points:
                break
            point_ids = [str(point.id) for point in points]
            payloads_by_id, missing_ids, fallback_count, repair_count = await _retrieve_payloads_resilient(point_ids)
            skipped_hydration_ids.extend(missing_ids)
            sqlite_hydration_fallbacks += fallback_count
            qdrant_payload_auto_repairs += repair_count
            for point in points:
                payload = payloads_by_id.get(str(point.id))
                if payload is None:
                    continue
                scanned_memories += 1
                if _is_law_like_payload(payload):
                    governance_records.append({
                        "store_name": "qdrant_memories",
                        "record_locator": str(point.id),
                        "payload": payload,
                    })
                result = classify_memory_payload(payload)
                classified[result["dataset_class"]] = classified.get(result["dataset_class"], 0) + 1
                actions[result["recommended_action"]] = actions.get(result["recommended_action"], 0) + 1
                if result["recommended_action"] != "keep":
                    policy = policy_for_dataset_class(result["dataset_class"])
                    store.upsert_finding(
                        finding_id=f"qdrant:{point.id}:{result['dataset_class']}",
                        store_name="qdrant_memories",
                        record_locator=str(point.id),
                        dataset_class=result["dataset_class"],
                        recommended_action=result["recommended_action"],
                        exclude_from_learning=bool(result["exclude_from_learning"]),
                        confidence=float(result["confidence"]),
                        reasons=list(result["reasons"]),
                        details={
                            "category": payload.get("category", ""),
                            "source": payload.get("source", ""),
                            "project": payload.get("project", ""),
                            "tags": list(payload.get("tags") or []),
                            "policy": policy,
                        },
                    )
                    findings_count += 1
                else:
                    resolved_stale_findings += store.resolve_open_findings_for_record(
                        store_name="qdrant_memories",
                        record_locator=str(point.id),
                        reason=f"current_classification_keep:{result['dataset_class']}",
                    )
            remaining -= len(point_ids)
            if next_offset is None:
                qdrant_wrapped = True
                break
            offset = next_offset
    except Exception as exc:
        logger.warning("Data hygiene audit fallback to SQLite memory store after qdrant scroll failure: %s", exc)
        memory_scan_source = "sqlite_memory_store_fallback"
        qdrant_scan_error = str(exc)
        rows = await get_memory_store().list_by_category("memory", limit=max(memory_limit, 100))
        for row in rows[:memory_limit]:
            scanned_memories += 1
            metadata = dict(row.get("metadata") or {})
            payload = dict(metadata)
            payload.setdefault("category", metadata.get("category") or row.get("category") or "")
            payload.setdefault("content", row.get("content") or "")
            locator = str(row.get("memory_id") or "")
            if _is_law_like_payload(payload):
                governance_records.append({
                    "store_name": "qdrant_memories",
                    "record_locator": locator,
                    "payload": payload,
                })
            result = classify_memory_payload(payload)
            classified[result["dataset_class"]] = classified.get(result["dataset_class"], 0) + 1
            actions[result["recommended_action"]] = actions.get(result["recommended_action"], 0) + 1
            if result["recommended_action"] != "keep":
                policy = policy_for_dataset_class(result["dataset_class"])
                store.upsert_finding(
                    finding_id=f"qdrant:{locator}:{result['dataset_class']}",
                    store_name="qdrant_memories",
                    record_locator=locator,
                    dataset_class=result["dataset_class"],
                    recommended_action=result["recommended_action"],
                    exclude_from_learning=bool(result["exclude_from_learning"]),
                    confidence=float(result["confidence"]),
                    reasons=list(result["reasons"]),
                    details={
                        "category": payload.get("category", ""),
                        "source": payload.get("source", ""),
                        "project": payload.get("project", ""),
                        "tags": list(payload.get("tags") or []),
                        "policy": policy,
                        "fallback_source": "sqlite_memory_store",
                    },
                )
                findings_count += 1
            else:
                resolved_stale_findings += store.resolve_open_findings_for_record(
                    store_name="qdrant_memories",
                    record_locator=locator,
                    reason=f"current_classification_keep:{result['dataset_class']}",
                )

    governance_duplicate_packets = detect_governance_duplicate_review_packets(governance_records)
    governance_duplicate_findings = 0
    for packet in governance_duplicate_packets:
        signature_hash = str(packet.get("signature_hash") or "")
        for sample in packet.get("sample_records") or []:
            locator = str(sample.get("record_locator") or "")
            if not locator:
                continue
            store.upsert_finding(
                finding_id=f"governance_duplicate:{signature_hash}:{locator}",
                store_name=str(sample.get("store_name") or "qdrant_memories"),
                record_locator=locator,
                dataset_class="governance_duplicate",
                recommended_action="operator-review",
                exclude_from_learning=True,
                confidence=0.86,
                reasons=[
                    str(packet.get("suspicion_type") or "duplicate_governance_artifact"),
                    "requires_operator_review:no_silent_merge_or_delete",
                ],
                details={
                    "category": "law",
                    "project": sample.get("project", ""),
                    "scope": sample.get("scope", ""),
                    "title": sample.get("title", ""),
                    "review_packet": packet,
                    "policy": policy_for_dataset_class("governance_duplicate"),
                },
            )
            governance_duplicate_findings += 1
    if governance_duplicate_findings:
        findings_count += governance_duplicate_findings
        classified["governance_duplicate"] = classified.get("governance_duplicate", 0) + governance_duplicate_findings
        actions["operator-review"] = actions.get("operator-review", 0) + governance_duplicate_findings

    events = await get_learning_store().list_events(
        limit=event_limit,
        before_ts=event_before_ts,
        before_id=event_before_id,
    )
    next_event_before_ts = event_before_ts
    next_event_before_id = event_before_id
    events_wrapped = False
    for event in events:
        scanned_events += 1
        result = classify_learning_event(event)
        classified[result["dataset_class"]] = classified.get(result["dataset_class"], 0) + 1
        actions[result["recommended_action"]] = actions.get(result["recommended_action"], 0) + 1
        if result["recommended_action"] != "keep":
            policy = policy_for_dataset_class(result["dataset_class"])
            store.upsert_finding(
                finding_id=f"learning_event:{event['id']}:{result['dataset_class']}",
                store_name="learning_events",
                record_locator=str(event["id"]),
                dataset_class=result["dataset_class"],
                recommended_action=result["recommended_action"],
                exclude_from_learning=bool(result["exclude_from_learning"]),
                confidence=float(result["confidence"]),
                reasons=list(result["reasons"]),
                details={
                    "event_type": event.get("event_type", ""),
                    "project": event.get("project", ""),
                    "context_signature": event.get("context_signature", ""),
                    "policy": policy,
                },
            )
            findings_count += 1
        else:
            resolved_stale_findings += store.resolve_open_findings_for_record(
                store_name="learning_events",
                record_locator=str(event["id"]),
                reason=f"current_classification_keep:{result['dataset_class']}",
            )
    if events:
        oldest_event = events[-1]
        next_event_before_ts = float(oldest_event.get("ts") or 0.0)
        next_event_before_id = int(oldest_event.get("id") or 0)
    else:
        next_event_before_ts = None
        next_event_before_id = None
        if event_before_ts is not None or event_before_id is not None:
            events_wrapped = True

    finished_at = time.time()
    status = "warning" if (findings_count or memory_scan_source != "qdrant") else "ok"
    audit_id = str(uuid.uuid4())
    details = {
        "memory_limit": memory_limit,
        "event_limit": event_limit,
        "scanned_memories": scanned_memories,
        "scanned_events": scanned_events,
        "findings_count": findings_count,
        "resolved_stale_findings": resolved_stale_findings,
        "governance_duplicate_packets": len(governance_duplicate_packets),
        "governance_duplicate_findings": governance_duplicate_findings,
        "memory_scan_source": memory_scan_source,
        "qdrant_scan_start_offset": qdrant_offset,
        "qdrant_scan_next_offset": offset if not qdrant_wrapped else None,
        "qdrant_scan_wrapped": qdrant_wrapped,
        "event_scan_start_before_ts": event_before_ts,
        "event_scan_start_before_id": event_before_id,
        "event_scan_next_before_ts": next_event_before_ts,
        "event_scan_next_before_id": next_event_before_id,
        "event_scan_wrapped": events_wrapped,
    }
    if skipped_hydration_ids:
        details["qdrant_hydration_skipped"] = len(skipped_hydration_ids)
        details["qdrant_hydration_skipped_sample"] = skipped_hydration_ids[:10]
    if sqlite_hydration_fallbacks:
        details["qdrant_sqlite_hydration_fallbacks"] = sqlite_hydration_fallbacks
    if qdrant_payload_auto_repairs:
        details["qdrant_payload_auto_repairs"] = qdrant_payload_auto_repairs
    if qdrant_scan_error:
        details["qdrant_scan_error"] = qdrant_scan_error
    latest = store.record_audit(
        audit_id=audit_id,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        total_records=scanned_memories + scanned_events,
        classified=classified,
        actions=actions,
        details=details,
    )
    return {
        "status": status,
        "audit_id": audit_id,
        "classified": classified,
        "actions": actions,
        "scanned_memories": scanned_memories,
        "scanned_events": scanned_events,
        "findings_count": findings_count,
        "latest_audit": latest,
        "next_qdrant_offset": offset if not qdrant_wrapped else None,
        "next_event_before_ts": next_event_before_ts,
        "next_event_before_id": next_event_before_id,
        "wrapped": {
            "qdrant": qdrant_wrapped,
            "events": events_wrapped,
        },
    }


async def queue_hygiene_remediation(
    *,
    recommended_action: str,
    requested_by: str,
    queue,
    store_name: str | None = None,
    dataset_class: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    import uuid

    store = get_data_hygiene_store()
    config = HYGIENE_REMEDIATION_REGISTRY.get(recommended_action)
    if not config:
        raise ValueError(f"No remediation is registered for action {recommended_action}")
    findings = store.list_open_findings_for_action(
        recommended_action=recommended_action,
        store_name=store_name,
        dataset_class=dataset_class,
        limit=limit,
    )
    if not findings:
        scope = f" and dataset_class {dataset_class}" if dataset_class else ""
        raise ValueError(f"No open findings exist for action {recommended_action}{scope}")
    job_id = await queue.submit(
        config["job_type"],
        {
            "recommended_action": recommended_action,
            "store_name": store_name or "",
            "dataset_class": dataset_class or "",
            "finding_ids": [item["finding_id"] for item in findings],
            "records": [
                {
                    "finding_id": item["finding_id"],
                    "store_name": item["store_name"],
                    "record_locator": item["record_locator"],
                    "dataset_class": item["dataset_class"],
                    "details": item.get("details", {}),
                }
                for item in findings
            ],
        },
    )
    return store.queue_remediation(
        remediation_id=str(uuid.uuid4()),
        recommended_action=recommended_action,
        store_name=store_name or "",
        requested_by=requested_by,
        job_id=job_id,
        details={
            "description": config["description"],
            "dataset_class": dataset_class or "",
            "finding_ids": [item["finding_id"] for item in findings],
            "scope_summary": build_hygiene_scope_summary(findings),
        },
    )


def compact_hygiene_remediation(item: dict[str, Any], *, sample_size: int = 5) -> dict[str, Any]:
    details = item.get("details") or {}
    finding_ids = [str(value) for value in (details.get("finding_ids") or []) if str(value)]
    closure_summary = details.get("closure_summary") or {}
    compact = {key: value for key, value in item.items() if key != "details"}
    compact["details_summary"] = {
        "description": str(details.get("description") or ""),
        "dataset_class": str(details.get("dataset_class") or ""),
        "finding_count": len(finding_ids),
        "sample_finding_ids": finding_ids[:sample_size],
    }
    if details.get("scope_summary"):
        compact["details_summary"]["scope_summary"] = details.get("scope_summary")
    if closure_summary:
        compact["details_summary"]["closure_summary"] = closure_summary
    if details.get("closure_checked_at"):
        compact["details_summary"]["closure_checked_at"] = details.get("closure_checked_at")
    return compact


async def queue_reviewed_delete_remediation(
    *,
    requested_by: str,
    queue,
    limit: int = 500,
) -> dict[str, Any]:
    import uuid

    store = get_data_hygiene_store()
    findings = [
        item for item in store.list_findings(recommended_action="delete", limit=5000)
        if item.get("status") == "quarantine_candidate"
    ][:limit]
    if not findings:
        raise ValueError("No quarantine_candidate findings exist for reviewed delete")
    job_id = await queue.submit(
        "data_hygiene_reviewed_delete",
        {
            "finding_ids": [item["finding_id"] for item in findings],
            "records": [
                {
                    "finding_id": item["finding_id"],
                    "store_name": item["store_name"],
                    "record_locator": item["record_locator"],
                    "dataset_class": item["dataset_class"],
                    "details": item.get("details", {}),
                }
                for item in findings
            ],
        },
    )
    return store.queue_remediation(
        remediation_id=str(uuid.uuid4()),
        recommended_action="delete-reviewed",
        store_name="reviewed",
        requested_by=requested_by,
        job_id=job_id,
        details={
            "description": HYGIENE_REMEDIATION_REGISTRY["delete-reviewed"]["description"],
            "finding_ids": [item["finding_id"] for item in findings],
            "scope_summary": build_hygiene_scope_summary(findings),
        },
    )


async def queue_approved_delete_remediation(
    *,
    requested_by: str,
    queue,
    store_name: str = "qdrant_memories",
    limit: int = 500,
) -> dict[str, Any]:
    import uuid

    store = get_data_hygiene_store()
    findings = [
        item for item in store.list_findings(store_name=store_name, recommended_action="delete", limit=5000)
        if item.get("status") == "quarantined"
    ][:limit]
    if not findings:
        raise ValueError("No quarantined delete findings exist for approved delete")
    job_id = await queue.submit(
        "data_hygiene_approved_delete",
        {
            "finding_ids": [item["finding_id"] for item in findings],
            "records": [
                {
                    "finding_id": item["finding_id"],
                    "store_name": item["store_name"],
                    "record_locator": item["record_locator"],
                    "dataset_class": item["dataset_class"],
                    "details": item.get("details", {}),
                }
                for item in findings
            ],
        },
    )
    return store.queue_remediation(
        remediation_id=str(uuid.uuid4()),
        recommended_action="delete-approved",
        store_name=store_name,
        requested_by=requested_by,
        job_id=job_id,
        details={
            "description": HYGIENE_REMEDIATION_REGISTRY["delete-approved"]["description"],
            "finding_ids": [item["finding_id"] for item in findings],
            "scope_summary": build_hygiene_scope_summary(findings),
        },
    )


async def reconcile_completed_remediations(*, queue) -> dict[str, Any]:
    store = get_data_hygiene_store()
    remediations = store.list_remediations(status="done", limit=500)
    reconciled = 0
    resolved = 0
    archived = 0
    for remediation in remediations:
        details = remediation.get("details", {})
        if details.get("closure_checked_at"):
            continue
        job = queue.get_job(remediation.get("job_id")) if remediation.get("job_id") else None
        result = (job or {}).get("result") or {}
        finding_ids = list(details.get("finding_ids") or result.get("finding_ids") or [])
        if remediation["recommended_action"] == "exclude-from-learning":
            target_status = "resolved"
        elif remediation["recommended_action"] == "archive":
            target_status = "archived"
        else:
            target_status = "resolved"
        affected = 0
        for finding_id in finding_ids:
            item = store.set_finding_status(finding_id=finding_id, status=target_status)
            if item:
                affected += 1
        if target_status == "resolved":
            resolved += affected
        else:
            archived += affected
        store.patch_remediation_details(
            remediation_id=remediation["remediation_id"],
            patch={
                "closure_checked_at": time.time(),
                "closure_summary": {
                    "affected_findings": affected,
                    "target_status": target_status,
                },
            },
        )
        reconciled += 1
    return {
        "reconciled": reconciled,
        "resolved_findings": resolved,
        "archived_findings": archived,
    }


def findings_for_manual_review(*, limit: int = 200) -> list[dict[str, Any]]:
    store = get_data_hygiene_store()
    items = store.list_findings(limit=2000)
    selected: list[dict[str, Any]] = []
    for item in items:
        policy = policy_for_dataset_class(item["dataset_class"])
        if policy.get("manual_review_required") and item.get("status") in {"open", "manual_review", "quarantine_candidate"}:
            selected.append(item)
    return selected[:limit]


def _looks_like_strong_test_trace(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    return any(
        marker in raw
        for marker in (
            "pytest",
            "synthetic",
            "fixture",
            "demo-fixture",
            "integration-test",
            "unittest",
            "benchmark",
            "fake data",
            "mock data",
        )
    )


def is_auto_test_cleanup_candidate(item: dict[str, Any]) -> bool:
    if str(item.get("dataset_class") or "") != "synthetic_test":
        return False
    if str(item.get("recommended_action") or "") != "delete":
        return False

    reasons = [str(reason).strip().lower() for reason in (item.get("reasons") or []) if str(reason).strip()]
    for reason in reasons:
        if reason == "synthetic_text_marker":
            return True
        if reason.startswith("synthetic_marker:"):
            marker = reason.split(":", 1)[1].strip()
            if marker in _AUTO_TEST_CLEANUP_MARKERS:
                return True

    details = item.get("details") or {}
    if _looks_like_strong_test_trace(str(details.get("source") or "")):
        return True
    if _looks_like_strong_test_trace(str(details.get("project") or "")):
        return True
    if _looks_like_strong_test_trace(str(details.get("context_signature") or "")):
        return True
    if _looks_like_strong_test_trace(str(details.get("event_type") or "")):
        return True

    for tag in (details.get("tags") or []):
        tag_text = str(tag).strip().lower()
        if tag_text in _AUTO_TEST_CLEANUP_MARKERS:
            return True
        if _looks_like_strong_test_trace(tag_text):
            return True
    return False


def promote_auto_test_cleanup_candidates(
    *,
    limit: int = 500,
    include_qdrant: bool = True,
    include_learning_events: bool = True,
) -> dict[str, Any]:
    store = get_data_hygiene_store()
    rows = store.list_findings(dataset_class="synthetic_test", recommended_action="delete", limit=max(limit * 10, 2000))
    candidate_rows: list[dict[str, Any]] = []
    for item in rows:
        status = str(item.get("status") or "")
        if status not in {"open", "manual_review", "quarantine_candidate", "quarantined"}:
            continue
        if not is_auto_test_cleanup_candidate(item):
            continue
        store_name = str(item.get("store_name") or "")
        if store_name == "qdrant_memories" and not include_qdrant:
            continue
        if store_name == "learning_events" and not include_learning_events:
            continue
        candidate_rows.append(item)
        if len(candidate_rows) >= limit:
            break

    updated = 0
    updated_ids: list[str] = []
    ready_for_reviewed_delete = 0
    ready_for_approved_delete = 0
    skipped = 0
    for item in candidate_rows:
        store_name = str(item.get("store_name") or "")
        status = str(item.get("status") or "")
        finding_id = str(item.get("finding_id") or "")

        if store_name == "learning_events":
            target_status = "quarantine_candidate"
        elif store_name == "qdrant_memories":
            target_status = "quarantined"
        else:
            skipped += 1
            continue

        final_status = status
        if status != target_status:
            changed = store.set_finding_status(finding_id=finding_id, status=target_status)
            if changed:
                updated += 1
                updated_ids.append(finding_id)
                final_status = target_status
            else:
                skipped += 1
                continue

        if store_name == "learning_events" and final_status == "quarantine_candidate":
            ready_for_reviewed_delete += 1
        if store_name == "qdrant_memories" and final_status == "quarantined":
            ready_for_approved_delete += 1

    return {
        "matched": len(rows),
        "eligible": len(candidate_rows),
        "updated": updated,
        "updated_ids": updated_ids,
        "ready_for_reviewed_delete": ready_for_reviewed_delete,
        "ready_for_approved_delete": ready_for_approved_delete,
        "skipped": skipped,
    }


def is_governed_synthetic_false_positive(item: dict[str, Any]) -> bool:
    if str(item.get("dataset_class") or "") != "synthetic_test":
        return False
    if str(item.get("recommended_action") or "") != "delete":
        return False
    details = item.get("details") or {}
    category = str(details.get("category") or "").strip().lower()
    source = str(details.get("source") or "").strip().lower()
    tags = [str(tag).strip().lower() for tag in (details.get("tags") or [])]
    return bool(_governed_payload_class(category, source, tags))


def resolve_governed_synthetic_false_positives(*, limit: int = 500) -> dict[str, Any]:
    store = get_data_hygiene_store()
    rows = store.list_findings(dataset_class="synthetic_test", recommended_action="delete", status="open", limit=max(limit * 10, 2000))
    updated_ids: list[str] = []
    skipped = 0
    for item in rows:
        if len(updated_ids) >= limit:
            break
        if not is_governed_synthetic_false_positive(item):
            skipped += 1
            continue
        changed = store.set_finding_status(finding_id=item["finding_id"], status="resolved")
        if changed:
            store.patch_finding_details(
                finding_id=item["finding_id"],
                patch={"resolved_by": "governed_synthetic_false_positive"},
                append_reasons=["governed_synthetic_false_positive"],
            )
            updated_ids.append(item["finding_id"])
        else:
            skipped += 1
    return {
        "matched": len(rows),
        "updated": len(updated_ids),
        "skipped": skipped,
        "finding_ids": updated_ids,
    }


def bulk_update_finding_statuses(
    *,
    target_status: str,
    current_status: str | None = None,
    dataset_class: str | None = None,
    recommended_action: str | None = None,
    store_name: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    store = get_data_hygiene_store()
    items = store.list_findings(
        store_name=store_name,
        dataset_class=dataset_class,
        recommended_action=recommended_action,
        status=current_status,
        limit=limit,
    )
    updated: list[str] = []
    skipped = 0
    for item in items:
        policy = policy_for_dataset_class(item["dataset_class"])
        if target_status in {"quarantine_candidate", "manual_review"} and not policy.get("manual_review_required"):
            skipped += 1
            continue
        changed = store.set_finding_status(finding_id=item["finding_id"], status=target_status)
        if changed:
            updated.append(item["finding_id"])
        else:
            skipped += 1
    return {
        "target_status": target_status,
        "matched": len(items),
        "updated": len(updated),
        "skipped": skipped,
        "finding_ids": updated,
    }


def build_workflow_summary(*, limit: int = 1000, current_project: str | None = None) -> dict[str, Any]:
    store = get_data_hygiene_store()
    items = store.list_findings(limit=limit)
    manual_review: dict[str, int] = {}
    quarantine_candidates: dict[str, int] = {}
    quarantined: dict[str, int] = {}
    delete_ready: dict[str, int] = {}
    for item in items:
        dataset_class = item["dataset_class"]
        policy = policy_for_dataset_class(dataset_class)
        if policy.get("manual_review_required") and item.get("status") in {"open", "manual_review", "quarantine_candidate"}:
            manual_review[dataset_class] = manual_review.get(dataset_class, 0) + 1
        if item.get("status") == "quarantine_candidate":
            quarantine_candidates[dataset_class] = quarantine_candidates.get(dataset_class, 0) + 1
        if item.get("status") == "quarantined":
            quarantined[dataset_class] = quarantined.get(dataset_class, 0) + 1
            if item.get("recommended_action") == "delete":
                delete_ready[dataset_class] = delete_ready.get(dataset_class, 0) + 1
    scope_summary = build_hygiene_scope_summary(
        [
            item for item in items
            if item.get("status") in {"open", "manual_review", "quarantine_candidate", "quarantined"}
        ],
        current_project=current_project,
    )
    next_actions = [
        "Review manual_review_required findings and move selected records to quarantine_candidate.",
        "Promote confirmed delete candidates from quarantine_candidate to quarantined.",
        "Run delete-dry-run before remediate-approved-delete for live qdrant memories.",
    ]
    if scope_summary.get("warnings"):
        next_actions.insert(
            0,
            "Review hygiene scope warnings before treating maintenance as current-project work.",
        )
    return {
        "manual_review_pending": manual_review,
        "quarantine_candidates": quarantine_candidates,
        "quarantined": quarantined,
        "delete_ready": delete_ready,
        "scope_summary": scope_summary,
        "next_actions": next_actions,
    }


def build_maintenance_suggestion(*, current_project: str | None = None, limit: int = 1000) -> dict[str, Any]:
    store = get_data_hygiene_store()
    overview = store.overview(current_project=current_project)
    workflow = build_workflow_summary(limit=limit, current_project=current_project)
    scope_summary = workflow.get("scope_summary") if isinstance(workflow.get("scope_summary"), dict) else {}
    active_findings = int(overview.get("active_findings") or 0)
    manual_review_pending = workflow.get("manual_review_pending") if isinstance(workflow.get("manual_review_pending"), dict) else {}
    quarantine_candidates = workflow.get("quarantine_candidates") if isinstance(workflow.get("quarantine_candidates"), dict) else {}
    delete_ready = workflow.get("delete_ready") if isinstance(workflow.get("delete_ready"), dict) else {}
    by_class = overview.get("by_class") if isinstance(overview.get("by_class"), dict) else {}
    by_action = overview.get("by_action") if isinstance(overview.get("by_action"), dict) else {}
    scope_warnings = scope_summary.get("warnings") if isinstance(scope_summary.get("warnings"), list) else []

    if active_findings <= 0:
        return {
            "status": "ok",
            "scope": _maintenance_scope_notice(scope_summary=scope_summary, current_project=current_project),
            "why_it_matters": "No active hygiene pressure is currently known; normal project work can continue.",
            "next_safe_action": "Continue normal workflow; rerun hygiene audit only when storage trust or search quality looks suspicious.",
            "destructive_action_allowed": False,
        }

    next_action = "Review hygiene overview before promoting cleanup into current work."
    if scope_warnings:
        next_action = "Review scope warnings first; do not present system or other-project hygiene as current-project work."
    elif manual_review_pending:
        next_action = "Review manual-review hygiene findings before any destructive cleanup."
    elif quarantine_candidates:
        next_action = "Preview reviewed-delete remediation for quarantined synthetic/test traces before execution."
    elif delete_ready:
        next_action = "Run delete-dry-run before approved delete for live memories."

    return {
        "status": "warning",
        "active_findings": active_findings,
        "top_dataset_classes": _top_counts(by_class),
        "top_recommended_actions": _top_counts(by_action),
        "manual_review_pending": manual_review_pending,
        "quarantine_candidates": quarantine_candidates,
        "delete_ready": delete_ready,
        "scope": _maintenance_scope_notice(scope_summary=scope_summary, current_project=current_project),
        "sample_scope_warnings": scope_warnings[:3],
        "why_it_matters": (
            "Hygiene findings can pollute search, learned routes, context cues, and next-work selection; "
            "maintenance should be explicit and scoped before cleanup."
        ),
        "next_safe_action": next_action,
        "destructive_action_allowed": False,
        "expand_refs": [
            "admin:data-hygiene/workflow",
            "admin:data-hygiene/findings",
            "admin:data-hygiene/retention-report",
        ],
    }


def _top_counts(values: dict[str, int], *, limit: int = 3) -> dict[str, int]:
    ordered = sorted(
        ((str(key), int(value or 0)) for key, value in values.items()),
        key=lambda item: (-item[1], item[0]),
    )
    return {key: value for key, value in ordered[:limit] if value > 0}


def _maintenance_scope_notice(*, scope_summary: dict[str, Any], current_project: str | None = None) -> dict[str, Any]:
    by_relation = scope_summary.get("by_relation") if isinstance(scope_summary.get("by_relation"), dict) else {}
    relation = "system_or_unknown"
    if by_relation:
        relation = max(by_relation.items(), key=lambda item: int(item[1] or 0))[0]
    notice = "Hygiene maintenance may affect system-wide or cross-project data; confirm scope before cleanup."
    if current_project and relation == "current_project":
        notice = f"Hygiene findings appear scoped to current project {current_project}."
    elif current_project and relation == "outside_current_project":
        notice = f"Hygiene findings mostly target other projects, not current project {current_project}."
    elif current_project and relation == "system_or_unknown_scope":
        notice = f"Hygiene findings have system/unknown scope; do not treat them as current project {current_project} work."
    return {
        "current_project": str(current_project or ""),
        "dominant_relation": relation,
        "total_findings": int(scope_summary.get("total_findings") or 0),
        "notice": notice,
    }


def build_ai_hygiene_resolution_plan(*, limit: int = 1000, sample_size: int = 10) -> dict[str, Any]:
    store = get_data_hygiene_store()
    overview = store.overview()
    latest_audit = overview.get("latest_audit") or {}
    details = latest_audit.get("details") or {}
    qdrant_scan_error = str(details.get("qdrant_scan_error") or "").strip()
    memory_scan_source = str(details.get("memory_scan_source") or "")

    open_findings = store.list_findings(status="open", limit=limit)
    safe_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for item in open_findings:
        action = str(item.get("recommended_action") or "")
        if action not in {"exclude-from-learning", "archive"}:
            continue
        store_name = str(item.get("store_name") or "")
        key = (action, store_name)
        candidate = safe_candidates.setdefault(
            key,
            {
                "recommended_action": action,
                "store_name": store_name,
                "open_findings": 0,
                "sample_finding_ids": [],
                "auto_apply_allowed": True,
                "blocked_reason": "",
            },
        )
        candidate["open_findings"] += 1
        if len(candidate["sample_finding_ids"]) < sample_size:
            finding_id = str(item.get("finding_id") or "")
            if finding_id:
                candidate["sample_finding_ids"].append(finding_id)
        if qdrant_scan_error and store_name == "qdrant_memories":
            candidate["auto_apply_allowed"] = False
            candidate["blocked_reason"] = "qdrant_scan_error"

    safe_remediation_candidates = sorted(
        safe_candidates.values(),
        key=lambda row: (
            0 if row.get("auto_apply_allowed") else 1,
            -int(row.get("open_findings") or 0),
            str(row.get("recommended_action") or ""),
            str(row.get("store_name") or ""),
        ),
    )

    manual_review_items = findings_for_manual_review(limit=limit)
    workflow = build_workflow_summary(limit=limit)
    manual_review_pending_total = int(sum(int(v or 0) for v in (workflow.get("manual_review_pending") or {}).values()))
    delete_ready_total = int(sum(int(v or 0) for v in (workflow.get("delete_ready") or {}).values()))

    next_actions: list[str] = []
    if qdrant_scan_error:
        next_actions.append(
            "Qdrant scan failed during hygiene audit; keep qdrant_memories remediations in plan-only mode until scan health is restored."
        )
        next_actions.append(
            "Inspect qdrant logs/health, then rerun /api/v1/admin/data-hygiene/audit to clear qdrant_scan_error and refresh findings from qdrant."
        )
    if any(item.get("auto_apply_allowed") and int(item.get("open_findings") or 0) > 0 for item in safe_remediation_candidates):
        next_actions.append(
            "Queue safe remediations (exclude-from-learning/archive) for allowed stores, then run /api/v1/admin/data-hygiene/reconcile."
        )
    if manual_review_pending_total > 0:
        next_actions.append(
            "Manual review is still required for synthetic_test delete candidates before any destructive delete job."
        )
    if not next_actions:
        next_actions.append("No immediate hygiene remediation actions are pending.")

    return {
        "status": "warning" if (qdrant_scan_error or open_findings or manual_review_pending_total) else "ok",
        "qdrant_scan_error": qdrant_scan_error,
        "memory_scan_source": memory_scan_source,
        "summary": {
            "active_findings": int(overview.get("active_findings") or 0),
            "open_findings_scanned": len(open_findings),
            "safe_candidate_groups": len(safe_remediation_candidates),
            "manual_review_pending_total": manual_review_pending_total,
            "delete_ready_total": delete_ready_total,
        },
        "safe_remediation_candidates": safe_remediation_candidates,
        "manual_review_sample_ids": [
            str(item.get("finding_id") or "") for item in manual_review_items[:sample_size] if str(item.get("finding_id") or "")
        ],
        "workflow": workflow,
        "next_actions": next_actions,
    }


def build_operator_playbook(*, limit: int = 1000, current_project: str | None = None) -> dict[str, Any]:
    workflow = build_workflow_summary(limit=limit, current_project=current_project)
    manual_pending = workflow.get("manual_review_pending", {})
    quarantine_candidates = workflow.get("quarantine_candidates", {})
    delete_ready = workflow.get("delete_ready", {})

    steps: list[dict[str, Any]] = [
        {
            "stage": "review",
            "goal": "Inspect manual-review datasets before any destructive action.",
            "when": manual_pending,
            "read_endpoints": [
                "/api/v1/admin/data-hygiene/workflow",
                "/api/v1/admin/data-hygiene/manual-review",
                "/api/v1/admin/data-hygiene/retention-report",
            ],
            "write_endpoints": [
                "/api/v1/admin/data-hygiene/review/quarantine-synthetic",
                "/api/v1/admin/data-hygiene/review/bulk-status",
            ],
        },
        {
            "stage": "preview_reviewed_delete",
            "goal": "Preview reviewed synthetic/test deletes before executing the reviewed-delete job.",
            "when": quarantine_candidates,
            "read_endpoints": [
                "/api/v1/admin/data-hygiene/reviewed-delete-preview",
                "/api/v1/admin/data-hygiene/workflow",
            ],
            "write_endpoints": [
                "/api/v1/admin/data-hygiene/remediate-reviewed-delete",
                "/api/v1/admin/data-hygiene/reconcile",
            ],
        },
        {
            "stage": "preview_live_delete",
            "goal": "Preview approved delete for live qdrant memories only after explicit quarantine.",
            "when": delete_ready,
            "read_endpoints": [
                "/api/v1/admin/data-hygiene/delete-dry-run",
                "/api/v1/admin/data-hygiene/remediations",
            ],
            "write_endpoints": [
                "/api/v1/admin/data-hygiene/remediate-approved-delete",
                "/api/v1/admin/data-hygiene/reconcile",
            ],
        },
    ]
    return {
        "workflow": workflow,
        "principles": [
            "Hygiene maintenance can be current-project, other-project, multi-project, or system-wide; show scope before suggesting cleanup.",
            "Exclude noisy service and telemetry data from learning before attempting deletion.",
            "Synthetic/test data requires manual review before delete.",
            "Use preview endpoints before any destructive remediation job.",
            "Always run reconcile after a remediation job completes.",
        ],
        "steps": steps,
    }


def build_retention_report(*, limit: int = 1000) -> dict[str, Any]:
    store = get_data_hygiene_store()
    items = store.list_findings(limit=limit)
    by_dataset: dict[str, dict[str, Any]] = {}
    delete_candidates = 0
    manual_review = 0
    for item in items:
        policy = policy_for_dataset_class(item["dataset_class"])
        bucket = by_dataset.setdefault(
            item["dataset_class"],
            {
                "count": 0,
                "recommended_action": item["recommended_action"],
                "retention": policy["retention"],
                "manual_review_required": policy["manual_review_required"],
            },
        )
        bucket["count"] += 1
        if item["recommended_action"] == "delete":
            delete_candidates += 1
        if policy["manual_review_required"] and item.get("status") in {"open", "manual_review", "quarantine_candidate"}:
            manual_review += 1
    return {
        "datasets": by_dataset,
        "delete_candidates": delete_candidates,
        "manual_review_pending": manual_review,
    }


def build_delete_dry_run(
    *,
    store_name: str = "qdrant_memories",
    status: str = "quarantined",
    limit: int = 500,
) -> dict[str, Any]:
    store = get_data_hygiene_store()
    items = [
        item for item in store.list_findings(store_name=store_name, recommended_action="delete", limit=5000)
        if item.get("status") == status
    ][:limit]
    sample = [
        {
            "finding_id": item["finding_id"],
            "record_locator": item["record_locator"],
            "dataset_class": item["dataset_class"],
            "confidence": item["confidence"],
            "reasons": item.get("reasons", []),
            "policy": policy_for_dataset_class(item["dataset_class"]),
        }
        for item in items[:20]
    ]
    return {
        "store_name": store_name,
        "required_status": status,
        "candidate_count": len(items),
        "sample": sample,
        "destructive": True,
        "requires_explicit_approval": True,
    }


def build_reviewed_delete_preview(
    *,
    store_name: str = "learning_events",
    status: str = "quarantine_candidate",
    limit: int = 500,
) -> dict[str, Any]:
    store = get_data_hygiene_store()
    items = [
        item for item in store.list_findings(store_name=store_name, recommended_action="delete", limit=5000)
        if item.get("status") == status
    ][:limit]
    sample = [
        {
            "finding_id": item["finding_id"],
            "record_locator": item["record_locator"],
            "dataset_class": item["dataset_class"],
            "confidence": item["confidence"],
            "reasons": item.get("reasons", []),
            "policy": policy_for_dataset_class(item["dataset_class"]),
        }
        for item in items[:20]
    ]
    return {
        "store_name": store_name,
        "required_status": status,
        "candidate_count": len(items),
        "sample": sample,
        "destructive": True,
        "requires_explicit_review": True,
    }


async def apply_reviewed_delete(payload: dict[str, Any]) -> dict[str, Any]:
    records = list(payload.get("records") or [])
    finding_ids = list(payload.get("finding_ids") or [])
    deleted = 0
    skipped = 0
    store = get_data_hygiene_store()
    learning_store = get_learning_store()
    for record in records:
        finding_id = str(record.get("finding_id") or "")
        finding = store.get_finding(finding_id) if finding_id else None
        if not finding:
            skipped += 1
            continue
        if finding.get("recommended_action") != "delete" or finding.get("status") != "quarantine_candidate":
            skipped += 1
            continue
        store_name = record.get("store_name")
        locator = str(record.get("record_locator") or "")
        if store_name == "learning_events" and locator:
            def _delete_event_sync() -> int:
                with learning_store._lock:
                    cur = learning_store._conn.execute("DELETE FROM events WHERE id = ?", (int(locator),))
                    learning_store._conn.commit()
                    return int(cur.rowcount or 0)
            deleted += await learning_store._run_sync(_delete_event_sync)
        else:
            skipped += 1
    return {
        "finding_ids": finding_ids,
        "deleted": deleted,
        "skipped_manual_only": skipped,
    }


async def apply_approved_delete(payload: dict[str, Any], qdrant) -> dict[str, Any]:
    from qdrant_client.http import models as qmodels
    from app.services.memory_store import get_memory_store

    records = list(payload.get("records") or [])
    finding_ids = list(payload.get("finding_ids") or [])
    deleted = 0
    skipped = 0
    hygiene_store = get_data_hygiene_store()
    content_store = get_memory_store()
    for record in records:
        finding_id = str(record.get("finding_id") or "")
        finding = hygiene_store.get_finding(finding_id) if finding_id else None
        if not finding:
            skipped += 1
            continue
        if finding.get("recommended_action") != "delete" or finding.get("status") != "quarantined":
            skipped += 1
            continue
        if record.get("store_name") != "qdrant_memories":
            skipped += 1
            continue
        locator = str(record.get("record_locator") or "")
        if not locator:
            skipped += 1
            continue
        try:
            await qdrant._client.delete(
                collection_name=settings.qdrant_collection_name,
                points_selector=qmodels.PointIdsList(points=[locator]),
            )
            await content_store.delete(locator)
            deleted += 1
        except Exception:
            skipped += 1
    return {
        "finding_ids": finding_ids,
        "deleted": deleted,
        "skipped_manual_only": skipped,
    }


_store: DataHygieneStore | None = None


def get_data_hygiene_store() -> DataHygieneStore:
    global _store
    if _store is None:
        _store = DataHygieneStore()
    return _store


def close_data_hygiene_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
