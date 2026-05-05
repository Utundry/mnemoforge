from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.services.improvements_store import get_improvements_store
from app.services.embedding_gateway import embed_query
from app.services.knowledge_language import canonicalize_agent_fields_to_english
from app.services.crystallization_service import list_canonicals
from app.services.law_service import build_law_context_block, list_project_laws
from app.services.learning_store import get_learning_store
from app.services.doc_section_service import list_doc_sections
from app.services.project_knowledge import ProjectKnowledgeService
from app.services.project_task_service import list_project_tasks
from app.services.qdrant_service import _point_to_record
from app.services.task_statement_service import build_task_statement_projection
from app.services.text_localization import normalize_text_for_display
from app.services.operational_instincts_service import (
    get_active_operational_instincts,
    render_operational_instincts_block,
)
from app.services.knowledge_projection_service import build_law_projection_block
from app.services.storage_trust_service import build_storage_trust_report


_TASK_CAPTURE_ARTIFACT_TYPES = (
    "task_capture_candidate",
    "decision_candidate",
    "chosen_decision",
    "code_link",
    "remaining_risk",
)


@dataclass(slots=True)
class ProjectContextBundle:
    project_id: str
    task: str
    context: str
    components: list[dict[str, Any]]
    laws: list[dict[str, Any]]
    improvements: list[dict[str, Any]]
    runtime_hints: list[dict[str, Any]]
    memoirs: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    promoted_canonicals: list[dict[str, Any]]
    task_triage: dict[str, Any]
    task_capture_candidates: list[dict[str, Any]]
    docs_sections: list[dict[str, Any]]
    operational_instincts: list[dict[str, Any]]
    coverage: dict[str, int]
    missing_sources: list[str]
    deferred_sources: list[str]
    recommended_mcp_calls: list[dict[str, Any]]
    code_inspection_recommended: bool
    message: str


@dataclass(slots=True)
class ProjectReadinessReport:
    project_id: str
    readiness_level: str
    readiness_score: int
    external_pilot_ready: bool
    coverage: dict[str, int]
    blocking_gaps: list[str]
    recommended_actions: list[str]
    strengths: list[str]
    operational_instincts: list[dict[str, Any]]
    snapshot: dict[str, Any] | None
    code_inspection_recommended: bool
    summary: str


@dataclass(slots=True)
class ProjectBootstrapChecklist:
    project_id: str
    readiness_level: str
    bootstrap_ready: bool
    next_step: str
    steps: list[dict[str, Any]]
    operational_instincts: list[dict[str, Any]]
    summary: str


class _QdrantAdapter:
    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        self._client = client
        self._collection = collection

    async def get(self, memory_id):
        results = await self._client.retrieve(
            collection_name=self._collection,
            ids=[str(memory_id)],
            with_payload=True,
            with_vectors=False,
        )
        if not results:
            raise ValueError("Memory not found")
        return _point_to_record(results[0])


def _is_qdrant_service_like(qdrant: Any) -> bool:
    return hasattr(qdrant, "_client") and hasattr(qdrant, "_collection")


def _signature_has_project(context_signature: str, project_id: str) -> bool:
    if not context_signature or not project_id:
        return False
    target = f"project={project_id}"
    return any(part.strip() == target for part in context_signature.split(";"))


def _hint_matches_project(row: dict[str, Any], project_id: str) -> bool:
    meta = row.get("meta") or {}
    if meta.get("project") == project_id or meta.get("project_id") == project_id:
        return True
    if _signature_has_project(str(row.get("context_signature") or ""), project_id):
        return True
    return f"project:{project_id}" in {str(tag) for tag in row.get("tags") or []}


def _hint_label(row: dict[str, Any]) -> str:
    meta = row.get("meta") or {}
    return str(meta.get("candidate_key") or row.get("workflow_action") or row.get("content")[:80]).strip()


_WEAK_HINT_LABELS = {"", "unknown", "hint", "runtime-hint", "legacy", "legacy-hint"}


def _is_runtime_hint_retrieval_worthy(row: dict[str, Any]) -> bool:
    meta = row.get("meta") or {}
    tags = {str(tag).strip().lower() for tag in row.get("tags") or []}
    project_ref = str(meta.get("project") or meta.get("project_id") or "").strip().lower()
    label = normalize_text_for_display(_hint_label(row))
    label_slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    content = normalize_text_for_display(str(row.get("content") or ""))
    observation = normalize_text_for_display(str(row.get("observation") or ""))
    why = normalize_text_for_display(str(row.get("why_it_matters") or ""))
    payload_blob = "\n".join(part for part in (label, content, observation, why) if part).lower()
    confidence = float(row.get("confidence") or 0.0)
    evidence_count = int(row.get("evidence_count") or 0)

    if not content:
        return False
    if "mnemoforge-demo" in payload_blob:
        return False
    if project_ref == "mnemoforge-demo":
        return False
    if "project:mnemoforge-demo" in tags:
        return False

    # Suppress weak legacy/demo artifacts that have little evidence.
    if label_slug in _WEAK_HINT_LABELS and (confidence < 0.7 or evidence_count < 2):
        return False
    if label_slug.startswith("legacy-") and (confidence < 0.8 or evidence_count < 2):
        return False
    if ("demo" in tags or "legacy" in tags) and evidence_count < 2:
        return False

    return True


def _memoir_title(row: dict[str, Any]) -> str:
    content = normalize_text_for_display(str(row.get("content") or ""))
    if not content:
        return "Task memoir"
    first_line = content.splitlines()[0].strip()
    if first_line.startswith("# Memoir:"):
        return first_line.replace("# Memoir:", "", 1).strip() or "Task memoir"
    return first_line[:120] or "Task memoir"


def _memoir_is_retrieval_worthy(row: dict[str, Any]) -> bool:
    meta = row.get("meta") or {}
    quality = str(meta.get("quality_status") or "").strip().lower()
    if quality:
        return quality in {"grounded", "partial"}
    content = normalize_text_for_display(str(row.get("content") or ""))
    if not content:
        return False
    if "Unknown task" in content or "_No changes recorded._" in content:
        return False
    return True


def _latest_task_change_summary(task: dict[str, Any]) -> tuple[str, str]:
    changes = task.get("changes") or []
    if not changes:
        return "", ""
    latest = changes[-1]
    return str(latest.get("change_type") or ""), str(latest.get("content") or "").strip()


async def _fetch_runtime_hints(project_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = await get_learning_store().list_artifacts(scope="runtime_hint", status="active", limit=max(limit * 8, 50))
    matched = [
        row
        for row in rows
        if _hint_matches_project(row, project_id) and _is_runtime_hint_retrieval_worthy(row)
    ]
    matched.sort(
        key=lambda row: (
            -(int(row.get("evidence_count") or 0)),
            -(float(row.get("confidence") or 0.0)),
            -float(row.get("updated_at") or row.get("created_at") or 0.0),
        )
    )
    items: list[dict[str, Any]] = []
    for row in matched[:limit]:
        original = {
            "label": _hint_label(row),
            "content": row.get("content") or "",
            "observation": row.get("observation") or "",
            "why_it_matters": row.get("why_it_matters") or "",
        }
        canonical = await canonicalize_agent_fields_to_english(original, allow_cloud=False)
        items.append(
            {
                "id": row["id"],
                "artifact_type": row.get("artifact_type") or "",
                "label": canonical["label"],
                "content": canonical["content"],
                "observation": canonical["observation"],
                "why_it_matters": canonical["why_it_matters"],
                "confidence": row.get("confidence") or 0.0,
                "evidence_count": row.get("evidence_count") or 0,
                "tags": row.get("tags") or [],
                "original": original,
            }
        )
    return items


async def _fetch_open_improvements(project_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = await get_improvements_store().list(project=project_id, status="open", limit=limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        original = {
            "title": normalize_text_for_display(row["title"]),
            "description": normalize_text_for_display(row["description"]),
        }
        canonical = await canonicalize_agent_fields_to_english(original, allow_cloud=False)
        items.append(
            {
                "id": row["id"],
                "title": canonical["title"],
                "description": canonical["description"],
                "importance_score": row.get("importance_score") or 0.0,
                "stage": row.get("stage") or "proposal",
                "verdict": row.get("verdict") or None,
                "tags": row.get("tags") or [],
                "original": original,
            }
        )
    return items


async def _fetch_improvements(project_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    rows = await get_improvements_store().list(project=project_id, status=status, limit=limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        original = {
            "title": normalize_text_for_display(row["title"]),
            "description": normalize_text_for_display(row["description"]),
        }
        canonical = await canonicalize_agent_fields_to_english(original, allow_cloud=False)
        items.append(
            {
                "id": row["id"],
                "title": canonical["title"],
                "description": canonical["description"],
                "importance_score": row.get("importance_score") or 0.0,
                "status": row.get("status") or "open",
                "stage": row.get("stage") or "proposal",
                "verdict": row.get("verdict") or None,
                "tags": row.get("tags") or [],
                "original": original,
            }
        )
    return items


async def _fetch_recent_memoirs(qdrant_client: AsyncQdrantClient, collection: str, project_id: str, limit: int = 3) -> list[dict[str, Any]]:
    from app.services.memoir_service import hydrate_memoir_payload_entries

    try:
        results, _ = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
                qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"project:{project_id}")),
            ]),
            limit=limit * 2,
            with_payload=True,
            with_vectors=False,
        )
        entries = [{"id": str(getattr(r, "id", "") or ""), "payload": dict(r.payload or {})} for r in results if r.payload]
        entries = await hydrate_memoir_payload_entries(entries)
        rows = [entry.get("payload") or {} for entry in entries if entry.get("payload")]
        rows.sort(key=lambda payload: payload.get("timestamp", ""), reverse=True)
        rows = [row for row in rows if _memoir_is_retrieval_worthy(row)][:limit]
    except Exception:
        rows = []
    items: list[dict[str, Any]] = []
    for row in rows:
        original = {
            "title": _memoir_title(row),
            "content": normalize_text_for_display(row.get("content") or ""),
        }
        canonical = await canonicalize_agent_fields_to_english(original, allow_cloud=False)
        items.append(
            {
                "title": canonical["title"],
                "timestamp": row.get("timestamp"),
                "task_id": next((str(tag).split(":", 1)[1] for tag in row.get("tags") or [] if str(tag).startswith("task_id:")), ""),
                "content": canonical["content"],
                "quality_status": (row.get("meta") or {}).get("quality_status") or "",
                "change_count": (row.get("meta") or {}).get("change_count") or 0,
                "original": original,
            }
        )
    return items


async def _fetch_recent_tasks(qdrant, project_id: str, limit: int = 3) -> list[dict[str, Any]]:
    rows = await list_project_tasks(qdrant, project=project_id, status="all", limit=max(limit * 4, 20))
    filtered = []
    for row in rows:
        if row.status == "archived":
            continue
        # Improvement bootstrap creates canonical task anchors before work begins.
        # Keep those entities for memoir/history, but do not duplicate them in
        # task context until they accumulate actual task activity or leave planning.
        if row.linked_improvement_id and row.status == "planning" and not row.changes:
            continue
        filtered.append(row)
    filtered.sort(key=_task_attention_sort_key)
    items: list[dict[str, Any]] = []
    for row in filtered[:limit]:
        change_type, change_summary = _latest_task_change_summary(row.model_dump())
        statement = await build_task_statement_projection(qdrant, project=project_id, task_id=row.task_id)
        original = {
            "title": row.title,
            "latest_change_summary": change_summary,
        }
        canonical = await canonicalize_agent_fields_to_english(original, allow_cloud=False)
        items.append(
            {
                "task_id": row.task_id,
                "title": canonical["title"],
                "status": row.status,
                "linked_improvement_id": row.linked_improvement_id,
                "updated_at": row.updated_at.isoformat(),
                "triage_reasons": _task_triage_reasons(row),
                "task_statement_incomplete": row.task_statement_incomplete,
                "task_capture_pending_count": row.task_capture_pending_count,
                "task_capture_promoted_count": row.task_capture_promoted_count,
                "latest_change_type": change_type,
                "latest_change_summary": canonical["latest_change_summary"],
                "next_actions": [item.model_dump() for item in statement.next_actions],
                "unresolved_ambiguities": list(statement.diff.unresolved_ambiguities),
                "original": original,
            }
        )
    return items


def _task_status_attention_rank(status: str) -> int:
    ranking = {
        "active": 0,
        "planning": 1,
        "paused": 2,
        "done": 3,
        "archived": 4,
    }
    return ranking.get(str(status or "").strip().lower(), 5)


def _task_attention_sort_key(row) -> tuple[int, int, int, str]:
    return (
        0 if row.task_statement_incomplete else 1,
        _task_status_attention_rank(row.status),
        -int(row.task_capture_pending_count or 0),
        str(row.updated_at.isoformat()),
    )


def _task_triage_reasons(row) -> list[str]:
    reasons: list[str] = []
    if row.task_statement_incomplete:
        reasons.append(f"incomplete_framing:{int(row.task_capture_pending_count or 0)}")
    if row.status == "active":
        reasons.append("active_task")
    elif row.status == "planning":
        reasons.append("planning_task")
    elif row.status == "paused":
        reasons.append("paused_task")
    if int(row.task_capture_promoted_count or 0) > 0:
        reasons.append(f"promoted_capture:{int(row.task_capture_promoted_count or 0)}")
    if row.changes:
        reasons.append("has_change_history")
    else:
        reasons.append("no_change_history")
    return reasons


async def build_task_triage(project_id: str, qdrant, *, limit: int = 5) -> dict[str, Any]:
    rows = await list_project_tasks(qdrant, project=project_id, status="all", limit=max(limit * 6, 30))
    candidates = []
    for row in rows:
        if row.status == "archived":
            continue
        if row.linked_improvement_id and row.status == "planning" and not row.changes:
            continue
        change_type, change_summary = _latest_task_change_summary(row.model_dump())
        statement = await build_task_statement_projection(qdrant, project=project_id, task_id=row.task_id)
        next_actions = [item.model_dump() for item in statement.next_actions]
        candidates.append(
            {
                "task_id": row.task_id,
                "title": row.title,
                "status": row.status,
                "updated_at": row.updated_at.isoformat(),
                "task_statement_incomplete": row.task_statement_incomplete,
                "task_capture_pending_count": int(row.task_capture_pending_count or 0),
                "task_capture_promoted_count": int(row.task_capture_promoted_count or 0),
                "latest_change_type": change_type,
                "latest_change_summary": change_summary,
                "next_actions": next_actions,
                "recommended_action": next((item.get("action") or "" for item in next_actions if item.get("action")), ""),
                "triage_reasons": _task_triage_reasons(row),
                "_sort_key": _task_attention_sort_key(row),
            }
        )
    candidates.sort(key=lambda item: item["_sort_key"])
    top = [{key: value for key, value in item.items() if key != "_sort_key"} for item in candidates[:limit]]
    return {
        "project_id": project_id,
        "found": len(top),
        "recommended_task_id": top[0]["task_id"] if top else "",
        "items": top,
    }


async def _fetch_task_capture_candidates(project_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    store = get_learning_store()
    for artifact_type in _TASK_CAPTURE_ARTIFACT_TYPES:
        rows.extend(
            await store.list_artifacts(
                artifact_type=artifact_type,
                scope="project",
                status="active",
                limit=max(limit * 8, 50),
            )
        )
    project_tag = f"project:{project_id}"
    items: list[dict[str, Any]] = []
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if project_tag not in tags:
            continue
        kind = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("capture_kind:")), "")
        if not kind:
            continue
        task_id = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("task_id:")), "")
        content = normalize_text_for_display(str(row.get("content") or ""))
        if not content:
            continue
        items.append(
            {
                "artifact_id": str(row.get("id") or ""),
                "task_id": task_id,
                "kind": kind,
                "artifact_type": str(row.get("artifact_type") or ""),
                "content": content,
                "source": next((tag.split(":", 1)[1] for tag in tags if tag.startswith("capture_source:")), ""),
                "confidence": float(row.get("confidence") or 0.0),
                "created_at": float(row.get("created_at") or 0.0),
                "observation": normalize_text_for_display(str(row.get("observation") or "")),
            }
        )
    items.sort(key=lambda row: (-row["created_at"], -row["confidence"]))
    return items[:limit]


async def _count_task_entities(qdrant, project_id: str) -> int:
    rows = await list_project_tasks(qdrant, project=project_id, status="all", limit=500)
    return sum(1 for row in rows if row.status != "archived")


def _build_component_context_block(components: list[dict[str, Any]]) -> str:
    if not components:
        return ""
    lines = ["## Relevant Components", ""]
    for row in components:
        lines.append(
            f"### {row.get('name')} ({row.get('component_id')})\n"
            f"**Purpose:** {row.get('purpose')}\n"
            f"**Implementation:** {row.get('implementation')}\n"
            f"**Key files:** {', '.join(row.get('key_files', []))}\n"
            f"**Endpoints:** {', '.join(row.get('endpoints', []))}\n"
        )
        if row.get("version_note"):
            lines.append(f"**Note:** {row.get('version_note')}\n")
        lines.append("")
    return "\n".join(lines).strip()


def _build_improvement_context_block(improvements: list[dict[str, Any]]) -> str:
    if not improvements:
        return ""
    lines = ["## Open Improvements", ""]
    for row in improvements:
        stage = str(row.get("stage") or "proposal")
        verdict = row.get("verdict")
        suffix = f" [stage:{stage}]"
        if verdict:
            suffix += f" [verdict:{verdict}]"
        lines.append(f"- {row['title']} (importance={row['importance_score']:.2f}){suffix}")
        if row.get("description"):
            lines.append(f"  Why open: {row['description']}")
    return "\n".join(lines)


def _build_runtime_hint_context_block(runtime_hints: list[dict[str, Any]]) -> str:
    if not runtime_hints:
        return ""
    lines = ["## Active Runtime Hints", ""]
    for row in runtime_hints:
        label = row.get("label") or row.get("content") or "runtime hint"
        lines.append(f"- {label} (confidence={float(row.get('confidence') or 0.0):.2f}, evidence={int(row.get('evidence_count') or 0)})")
        if row.get("content"):
            lines.append(f"  Guidance: {row['content']}")
        elif row.get("observation"):
            lines.append(f"  Observation: {row['observation']}")
    return "\n".join(lines)


def _build_memoir_context_block(memoirs: list[dict[str, Any]]) -> str:
    if not memoirs:
        return ""
    lines = ["## Recent Decision Memoirs", ""]
    for row in memoirs:
        title = row.get("title") or "Task memoir"
        ts = str(row.get("timestamp") or "")
        date = ts[:10] if ts else "unknown"
        lines.append(f"- {title} ({date})")
    return "\n".join(lines)


def _build_task_context_block(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return ""
    lines = ["## Recent Project Tasks", ""]
    for row in tasks:
        status_line = f"- {row['title']} [{row['status']}]"
        if row.get("task_statement_incomplete"):
            status_line += f" [incomplete-framing:{int(row.get('task_capture_pending_count') or 0)}]"
        elif int(row.get("task_capture_promoted_count") or 0) > 0:
            status_line += f" [promoted-capture:{int(row.get('task_capture_promoted_count') or 0)}]"
        lines.append(status_line)
        if row.get("latest_change_type") and row.get("latest_change_summary"):
            lines.append(f"  Latest change ({row['latest_change_type']}): {row['latest_change_summary']}")
        if row.get("task_statement_incomplete") and int(row.get("task_capture_pending_count") or 0) > 0:
            lines.append("  Recommended action: Review pending task capture drafts.")
            lines.append("  Next action: Review pending task capture drafts.")
        next_actions = list(row.get("next_actions") or [])
        if next_actions:
            first_action = str((next_actions[0] or {}).get("action") or "").strip()
            if first_action:
                lines.append(f"  Next action: {first_action}")
    return "\n".join(lines)


def _build_task_capture_context_block(task_capture_candidates: list[dict[str, Any]]) -> str:
    if not task_capture_candidates:
        return ""
    lines = ["## Task Capture Drafts", ""]
    for row in task_capture_candidates:
        task_label = row.get("task_id") or "unknown-task"
        source = row.get("source") or "capture"
        lines.append(
            f"- {row['kind']} for {task_label} "
            f"(source={source}, confidence={float(row.get('confidence') or 0.0):.2f})"
        )
        lines.append(f"  Draft: {row['content']}")
    return "\n".join(lines)


def _build_task_triage_context_block(task_triage: dict[str, Any]) -> str:
    items = list(task_triage.get("items") or [])
    if not items:
        return ""
    lines = ["## Task Triage", ""]
    recommended_task_id = str(task_triage.get("recommended_task_id") or "").strip()
    if recommended_task_id:
        lines.append(f"Recommended next task: {recommended_task_id}")
        recommended_item = next(
            (row for row in items if str(row.get("task_id") or "").strip() == recommended_task_id),
            None,
        )
        if recommended_item and str(recommended_item.get("recommended_action") or "").strip():
            lines.append(f"Recommended action: {recommended_item['recommended_action']}")
            lines.append(f"Next action: {recommended_item['recommended_action']}")
    for row in items[:3]:
        reason_text = ", ".join(str(reason).strip() for reason in row.get("triage_reasons") or [] if str(reason).strip())
        lines.append(f"- {row.get('title') or row.get('task_id') or 'task'} [{row.get('status') or 'unknown'}]")
        if reason_text:
            lines.append(f"  Why now: {reason_text}")
    return "\n".join(lines)


def _doc_projection_keys() -> tuple[str, ...]:
    return ("overview", "architecture", "decisions")


def _build_docs_context_block(docs_sections: list[dict[str, Any]]) -> str:
    if not docs_sections:
        return ""
    lines = ["## Current Documentation Projection", ""]
    for row in docs_sections:
        lines.append(f"### {row['name']}")
        if row.get("stale"):
            lines.append("_This projection is stale and may lag current component knowledge._")
        if row.get("projection_state") == "candidate":
            lines.append("_Using newer candidate documentation projection because the effective docs are older._")
        elif row.get("candidate_available"):
            lines.append("_A newer candidate documentation revision is available but not active yet._")
        if row.get("generated_at"):
            lines.append(f"_Generated: {row['generated_at'][:19]}Z_")
        lines.append(row["content"].strip())
        lines.append("")
    return "\n".join(lines).strip()


def _context_coverage(
    *,
    laws: list[dict[str, Any]],
    components: list[dict[str, Any]],
    improvements: list[dict[str, Any]],
    runtime_hints: list[dict[str, Any]],
    memoirs: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    docs_sections: list[dict[str, Any]],
    promoted_canonicals: list[dict[str, Any]] | None = None,
    deferred_sources: list[str] | None = None,
) -> tuple[dict[str, int], list[str], bool]:
    coverage = {
        "laws": len(laws),
        "components": len(components),
        "improvements": len(improvements),
        "runtime_hints": len(runtime_hints),
        "memoirs": len(memoirs),
        "tasks": len(tasks),
        "docs_sections": len(docs_sections),
        "promoted_canonicals": len(promoted_canonicals or []),
    }
    deferred = {str(item).strip() for item in (deferred_sources or []) if str(item).strip()}
    missing_sources = [name for name, count in coverage.items() if count == 0 and name not in deferred]
    code_inspection_recommended = coverage["components"] == 0 and coverage["docs_sections"] == 0
    return coverage, missing_sources, code_inspection_recommended


def _build_deferred_context_block(deferred_sources: list[str]) -> str:
    items = [str(item).strip() for item in deferred_sources if str(item).strip()]
    if not items:
        return ""
    lines = ["## Background Synthesis Deferred", ""]
    lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def _build_promoted_canonical_context_block(canonicals: list[dict[str, Any]]) -> str:
    if not canonicals:
        return ""
    lines = ["## Promoted Canonical Knowledge", ""]
    for row in canonicals:
        scope = str(row.get("scope") or "domain").strip()
        topic_path = str(row.get("topic_path") or "").strip()
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        confidence = float(row.get("confidence") or 0.0)
        status = str(row.get("canonical_status") or "active").strip()
        label = f"[{scope}] {topic_path}" if topic_path else f"[{scope}] canonical"
        lines.append(f"- {label}: {content}")
        lines.append(f"  Provenance: promoted canonical ({status}, confidence={confidence:.2f})")
    return "\n".join(lines)


def _build_recommended_mcp_calls(
    *,
    project_id: str,
    task: str,
    improvements: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    docs_sections: list[dict[str, Any]],
    coverage: dict[str, int],
    missing_sources: list[str],
    code_inspection_recommended: bool,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    task_text = str(task or "").casefold()

    if any(token in task_text for token in ("tool", "tools", "инструмент", "инструменты", "family", "families", "catalog", "catalogue", "каталог", "choose", "select")):
        calls.append(
            {
                "tool": "list_tool_families",
                "reason": "The task is about MCP/tool selection, so start with the compact family index instead of the full flat catalog.",
                "follow_up": "tool_feedback",
                "args": {
                    "include_compatibility_note": True,
                },
            }
        )

    if project_id and (improvements or tasks or missing_sources or docs_sections):
        calls.append(
            {
                "tool": "list_open_tasks",
                "reason": "Inspect open tasks on the unified artifact surface before branching into specialized paths.",
                "args": {
                    "project": project_id,
                    "limit": 10,
                },
            }
        )

    if code_inspection_recommended or (coverage.get("components", 0) == 0 and coverage.get("docs_sections", 0) == 0):
        calls.append(
            {
                "tool": "get_project_readiness",
                "reason": "The current bundle is sparse; readiness will tell you whether to bootstrap or inspect code next.",
                "args": {
                    "project_id": project_id,
                },
            }
        )

    open_improvement = next(
        (
            row
            for row in sorted(
                improvements,
                key=lambda item: (
                    -(float(item.get("importance_score") or 0.0)),
                    str(item.get("title") or ""),
                ),
            )
            if str(row.get("stage") or "proposal") != "stable"
            or not row.get("verdict")
        ),
        None,
    )
    if open_improvement and str(open_improvement.get("id") or "").strip():
        args: dict[str, Any] = {
            "improvement_id": str(open_improvement.get("id") or ""),
        }
        calls.append(
            {
                "tool": "review_improvement",
                "reason": f"Review the highest-value open improvement '{open_improvement.get('title')}' and set stage/verdict if the current work makes that possible.",
                "args": args,
            }
        )

    if not calls and task:
        calls.append(
            {
                "tool": "list_open_tasks",
                "reason": "Start from the open-task MCP surface when no better next step is obvious.",
                "args": {
                    "project": project_id,
                    "limit": 10,
                },
            }
        )

    return calls[:3]


def _build_recommended_mcp_calls_block(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return ""
    lines = ["## Recommended MCP Calls", ""]
    needs_feedback = False
    for idx, call in enumerate(calls, 1):
        tool = str(call.get("tool") or "unknown").strip()
        reason = str(call.get("reason") or "").strip()
        follow_up = str(call.get("follow_up") or "").strip()
        if follow_up == "tool_feedback":
            needs_feedback = True
        args = call.get("args") or {}
        arg_bits = []
        if isinstance(args, dict):
            for key in ("project", "project_id", "status", "type", "limit", "improvement_id", "stage"):
                value = args.get(key)
                if value not in (None, ""):
                    arg_bits.append(f"{key}={value}")
        line = f"{idx}. `{tool}`"
        if arg_bits:
            line += f" ({', '.join(arg_bits)})"
        lines.append(line)
        if reason:
            lines.append(f"   {reason}")
    if needs_feedback:
        lines.extend(
            [
                "",
                "After using a testing-stage tool, add a short `tool_feedback` report with the main friction or success signal.",
            ]
        )
    return "\n".join(lines)


def _filter_operational_instincts_for_recommended_calls(
    instincts: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    needs_tool_feedback = any(
        str(call.get("follow_up") or "").strip() == "tool_feedback"
        for call in calls
    )
    if needs_tool_feedback:
        return instincts
    filtered: list[dict[str, Any]] = []
    for row in instincts:
        searchable = " ".join(
            str(row.get(key) or "")
            for key in (
                "id",
                "instinct_id",
                "label",
                "content",
                "description",
                "guidance",
                "action",
                "why_it_matters",
                "failure_if_missing",
            )
        ).casefold()
        if "tool_feedback" in searchable:
            continue
        filtered.append(row)
    return filtered


def _clip_text(value: Any, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def build_enrich_available_layers(bundle: ProjectContextBundle) -> dict[str, dict[str, Any]]:
    return {
        "laws": {
            "available": bool(bundle.laws),
            "count": len(bundle.laws),
            "request": {"detail": "full"},
        },
        "components": {
            "available": bool(bundle.components),
            "count": len(bundle.components),
            "request": {"detail": "full", "max_components": len(bundle.components) or 3},
        },
        "improvements": {
            "available": bool(bundle.improvements),
            "count": len(bundle.improvements),
            "request": {"detail": "full"},
        },
        "runtime_hints": {
            "available": bool(bundle.runtime_hints),
            "count": len(bundle.runtime_hints),
            "request": {"detail": "full"},
        },
        "memoirs": {
            "available": bool(bundle.memoirs),
            "count": len(bundle.memoirs),
            "request": {"detail": "full"},
        },
        "tasks": {
            "available": bool(bundle.tasks),
            "count": len(bundle.tasks),
            "request": {"detail": "full"},
        },
        "task_triage": {
            "available": bool((bundle.task_triage or {}).get("items")),
            "count": len((bundle.task_triage or {}).get("items") or []),
            "request": {"detail": "full"},
        },
        "task_capture_candidates": {
            "available": bool(bundle.task_capture_candidates),
            "count": len(bundle.task_capture_candidates),
            "request": {"detail": "full"},
        },
        "docs_sections": {
            "available": bool(bundle.docs_sections),
            "count": len(bundle.docs_sections),
            "request": {"detail": "full"},
        },
        "promoted_canonicals": {
            "available": bool(bundle.promoted_canonicals),
            "count": len(bundle.promoted_canonicals),
            "request": {"detail": "full"},
        },
        "operational_instincts": {
            "available": bool(bundle.operational_instincts),
            "count": len(bundle.operational_instincts),
            "request": {"detail": "full"},
        },
        "recommended_mcp_calls": {
            "available": bool(bundle.recommended_mcp_calls),
            "count": len(bundle.recommended_mcp_calls),
            "request": {"detail": "full"},
        },
    }


def build_handoff_compact_enrich_context(bundle: ProjectContextBundle) -> str:
    lines = [
        f"## Project Context for: {bundle.task}",
        "",
        "## Handoff Compact Summary",
        "",
    ]
    coverage = bundle.coverage or {}
    if coverage:
        ordered = (
            "laws",
            "components",
            "improvements",
            "runtime_hints",
            "memoirs",
            "tasks",
            "docs_sections",
            "promoted_canonicals",
        )
        coverage_bits = [f"{key}={int(coverage.get(key) or 0)}" for key in ordered if key in coverage]
        lines.append("Coverage: " + ", ".join(coverage_bits))
    if bundle.message:
        lines.append(f"Note: {_clip_text(bundle.message, 260)}")
    if bundle.code_inspection_recommended:
        lines.append("Fallback: code inspection is recommended before making implementation decisions.")

    if bundle.laws:
        lines.extend(["", "## Applicable Project Laws", ""])
        for row in bundle.laws[:5]:
            title = row.get("title") or row.get("id") or "law"
            rationale = row.get("rationale") or row.get("statement") or ""
            lines.append(f"- {title}")
            if rationale:
                lines.append(f"  Why: {_clip_text(rationale, 180)}")

    if bundle.operational_instincts:
        lines.extend(["", "## Operational Instincts", ""])
        for row in bundle.operational_instincts[:3]:
            label = row.get("label") or row.get("content") or row.get("id") or "instinct"
            lines.append(f"- {_clip_text(label, 180)}")

    if bundle.components:
        lines.extend(["", "## Relevant Components", ""])
        for row in bundle.components[:3]:
            files = ", ".join(str(item) for item in row.get("key_files") or [] if str(item).strip())
            label = row.get("name") or row.get("component_id") or "component"
            suffix = f" - files: {files}" if files else ""
            lines.append(f"- {label} ({row.get('component_id') or 'unknown'}){suffix}")

    if bundle.improvements:
        lines.extend(["", "## Open Improvements", ""])
        for row in bundle.improvements[:3]:
            stage = row.get("stage") or "proposal"
            score = float(row.get("importance_score") or 0.0)
            lines.append(f"- {row.get('title') or row.get('id') or 'improvement'} [stage:{stage}, importance={score:.2f}]")
            if row.get("description"):
                lines.append(f"  Why open: {_clip_text(row.get('description'), 180)}")

    triage_items = list((bundle.task_triage or {}).get("items") or [])
    if triage_items:
        lines.extend(["", "## Task Triage", ""])
        recommended = str((bundle.task_triage or {}).get("recommended_task_id") or "").strip()
        if recommended:
            lines.append(f"Recommended task: {recommended}")
        for row in triage_items[:3]:
            title = row.get("title") or row.get("task_id") or "task"
            lines.append(f"- {title} [{row.get('status') or 'unknown'}]")
            action = str(row.get("recommended_action") or "").strip()
            if action:
                lines.append(f"  Action: {_clip_text(action, 180)}")
    elif bundle.tasks:
        lines.extend(["", "## Recent Project Tasks", ""])
        for row in bundle.tasks[:3]:
            lines.append(f"- {row.get('title') or row.get('task_id') or 'task'} [{row.get('status') or 'unknown'}]")

    if bundle.runtime_hints:
        lines.extend(["", "## Runtime Hints", ""])
        for row in bundle.runtime_hints[:3]:
            label = row.get("label") or row.get("content") or row.get("observation") or "runtime hint"
            lines.append(f"- {_clip_text(label, 180)}")

    if bundle.memoirs:
        lines.extend(["", "## Decision Memoirs", ""])
        for row in bundle.memoirs[:3]:
            ts = str(row.get("timestamp") or "")
            date = ts[:10] if ts else "unknown"
            lines.append(f"- {row.get('title') or 'Task memoir'} ({date})")

    if bundle.docs_sections:
        lines.extend(["", "## Documentation Projection Index", ""])
        for row in bundle.docs_sections[:5]:
            name = row.get("name") or row.get("section_key") or "section"
            state = row.get("projection_state") or "effective"
            lines.append(f"- {name} [{state}]")

    if bundle.task_capture_candidates:
        lines.extend(["", "## Task Capture Draft Index", ""])
        for row in bundle.task_capture_candidates[:3]:
            lines.append(f"- {row.get('kind') or 'draft'} for {row.get('task_id') or 'unknown-task'}")

    if bundle.promoted_canonicals:
        lines.extend(["", "## Promoted Canonical Index", ""])
        for row in bundle.promoted_canonicals[:3]:
            topic = row.get("topic_path") or row.get("scope") or "canonical"
            lines.append(f"- {topic}: {_clip_text(row.get('content'), 180)}")

    calls_block = _build_recommended_mcp_calls_block(bundle.recommended_mcp_calls)
    if calls_block:
        lines.extend(["", calls_block])

    if bundle.deferred_sources:
        lines.extend(["", _build_deferred_context_block(bundle.deferred_sources)])

    layers = build_enrich_available_layers(bundle)
    layer_bits = [
        f"{name}={meta['count']}"
        for name, meta in layers.items()
        if meta.get("available") and int(meta.get("count") or 0) > 0
    ]
    if layer_bits:
        lines.extend(["", "## Available Layers", "", "- " + ", ".join(layer_bits)])
        lines.append("- Request `detail=full` on `enrich_task_with_context` for full source text.")

    return "\n".join(line for line in lines if line is not None).strip()


def _canonical_signal(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("scope") or ""),
        str(row.get("topic_path") or ""),
        str(row.get("content") or ""),
    )


async def _fetch_promoted_canonicals(
    qdrant,
    ollama,
    task: str,
    *,
    project_id: str,
    limit: int = 3,
    force: bool = False,
) -> list[dict[str, Any]]:
    if not force:
        return []
    try:
        vector, _embedding_meta = await embed_query(
            task,
            primary=ollama,
            purpose="promoted_canonical_search",
        )
    except Exception as exc:
        logger.debug("Promoted canonical embedding failed: %s", exc)
        return []
    try:
        hits = await qdrant._client.search(
            collection_name=qdrant._collection,
            query_vector=vector,
            query_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="scope",
                        match=qmodels.MatchAny(any=["domain", "principle", "meta"]),
                    ),
                ]
            ),
            limit=max(limit * 4, 20),
            with_payload=True,
        )
    except Exception as exc:
        logger.debug("Promoted canonical search failed: %s", exc)
        return []

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in hits:
        record = hit if hasattr(hit, "id") else None
        try:
            payload = hit.payload or {}
        except Exception:
            payload = {}
        scope = str(payload.get("scope") or "")
        if scope not in {"domain", "principle", "meta"}:
            continue
        item = _point_to_record(hit)
        signal = _canonical_signal(
            {
                "scope": item.scope,
                "topic_path": item.topic_path,
                "content": item.content,
            }
        )
        if signal in seen:
            continue
        seen.add(signal)
        items.append(
            {
                "id": str(item.id),
                "scope": item.scope,
                "topic_path": item.topic_path or "",
                "content": item.content,
                "confidence": float(payload.get("confidence") or 0.0),
                "canonical_status": payload.get("canonical_status") or "active",
                "project": item.project,
                "timestamp": item.timestamp.isoformat(),
            }
        )
        if len(items) >= limit:
            break
    return items


async def _fetch_effective_doc_sections(
    qdrant_client,
    collection: str,
    project_id: str,
) -> list[dict[str, Any]]:
    # Local import prevents a module cycle with docs_service -> project_context_service.
    from app.services.docs_service import load_docs_cache

    cached = load_docs_cache(project_id)
    if (
        cached
        and cached.candidate_sections
        and cached.candidate_generated_at
        and cached.candidate_generated_at > cached.generated_at
    ):
        docs_sections: list[dict[str, Any]] = []
        for key in _doc_projection_keys():
            section = cached.candidate_sections.get(key) or cached.sections.get(key)
            if not section or not section.content.strip():
                continue
            docs_sections.append(
                {
                    "section_key": key,
                    "name": section.name,
                    "content": section.content.strip(),
                    "generated_at": (
                        cached.candidate_generated_at.isoformat()
                        if key in cached.candidate_sections
                        else cached.generated_at.isoformat()
                    ),
                    "candidate_available": True,
                    "projection_state": ("candidate" if key in cached.candidate_sections else "effective"),
                }
            )
        if docs_sections:
            return docs_sections

    try:
        rows = await list_doc_sections(qdrant_client, collection, project_id, limit=20)
    except Exception:
        rows = []

    if rows:
        docs_sections: list[dict[str, Any]] = []
        for row in rows:
            meta = row.get("meta") or {}
            section_key = str(meta.get("section_key") or "")
            if section_key not in _doc_projection_keys():
                continue
            docs_sections.append(
                {
                    "section_key": section_key,
                    "name": str(meta.get("section_name") or section_key.title()),
                    "content": str(row.get("content") or "").strip(),
                    "generated_at": str(meta.get("generated_at") or row.get("timestamp") or ""),
                    "candidate_available": bool(meta.get("candidate_available")),
                    "projection_state": "effective",
                }
            )
        if docs_sections:
            docs_sections.sort(key=lambda item: _doc_projection_keys().index(item["section_key"]))
            return docs_sections
    if not cached or not cached.sections:
        return []

    docs_sections: list[dict[str, Any]] = []
    candidate_available = bool(cached.candidate_sections)
    for key in _doc_projection_keys():
        section = cached.sections.get(key)
        if not section or not section.content.strip():
            continue
        docs_sections.append(
            {
                "section_key": key,
                "name": section.name,
                "content": section.content.strip(),
                "generated_at": cached.generated_at.isoformat(),
                "candidate_available": candidate_available,
                "projection_state": "effective",
            }
        )
    return docs_sections


async def assemble_project_context(
    *,
    project_id: str,
    task: str,
    qdrant,
    ollama,
    context_profile: str = "default",
    include_promoted_canonicals: bool = False,
    max_components: int = 3,
    max_improvements: int = 5,
    max_runtime_hints: int = 5,
    max_memoirs: int = 3,
) -> ProjectContextBundle:
    svc = ProjectKnowledgeService(qdrant._client, ollama)
    await svc.ensure_collection()
    resolved_profile = str(context_profile or "default").strip().lower() or "default"
    deferred_sources: list[str] = []
    promoted_canonicals: list[dict[str, Any]] = []
    if resolved_profile == "hot_path":
        deferred_sources = ["improvements", "memoirs", "docs_sections"]
        (
            components,
            laws,
            runtime_hints,
            tasks,
            task_triage,
            task_capture_candidates,
        ) = await asyncio.gather(
            svc.search(project_id, task, max_components),
            list_project_laws(
                qdrant,
                project=project_id,
                status="active",
                include_promoted=True,
                limit=20,
            ),
            _fetch_runtime_hints(project_id, limit=max_runtime_hints),
            _fetch_recent_tasks(qdrant, project_id, limit=3),
            build_task_triage(project_id, qdrant, limit=3),
            _fetch_task_capture_candidates(project_id, limit=5),
        )
        improvements = []
        memoirs = []
        docs_sections = []
    else:
        (
            components,
            laws,
            improvements,
            runtime_hints,
            memoirs,
            tasks,
            task_triage,
            task_capture_candidates,
            docs_sections,
        ) = await asyncio.gather(
            svc.search(project_id, task, max_components),
            list_project_laws(
                qdrant,
                project=project_id,
                status="active",
                include_promoted=True,
                limit=20,
            ),
            _fetch_open_improvements(project_id, limit=max_improvements),
            _fetch_runtime_hints(project_id, limit=max_runtime_hints),
            _fetch_recent_memoirs(qdrant._client, qdrant._collection, project_id, limit=max_memoirs),
            _fetch_recent_tasks(qdrant, project_id, limit=3),
            build_task_triage(project_id, qdrant, limit=3),
            _fetch_task_capture_candidates(project_id, limit=5),
            _fetch_effective_doc_sections(qdrant._client, qdrant._collection, project_id),
        )
    local_signal_count = sum(
        len(items or [])
        for items in (
            components,
            laws,
            improvements,
            runtime_hints,
            memoirs,
            tasks,
            docs_sections,
        )
    )
    include_promoted = bool(include_promoted_canonicals or local_signal_count <= 3)
    if include_promoted:
        promoted_canonicals = await _fetch_promoted_canonicals(
            qdrant,
            ollama,
            task,
            project_id=project_id,
            limit=3,
            force=True,
        )
    coverage, missing_sources, code_inspection_recommended = _context_coverage(
        laws=laws,
        components=components,
        improvements=improvements,
        runtime_hints=runtime_hints,
        memoirs=memoirs,
        tasks=tasks,
        docs_sections=docs_sections,
        promoted_canonicals=promoted_canonicals,
        deferred_sources=deferred_sources,
    )
    storage_trust_status = build_storage_trust_report().get("status", "ok")
    operational_instincts = get_active_operational_instincts(
        context_type="task_enrichment",
        project_id=project_id,
        storage_trust_status=storage_trust_status,
        code_inspection_recommended=code_inspection_recommended,
        limit=5,
    )
    recommended_mcp_calls = _build_recommended_mcp_calls(
        project_id=project_id,
        task=task,
        improvements=improvements,
        tasks=tasks,
        docs_sections=docs_sections,
        coverage=coverage,
        missing_sources=missing_sources,
        code_inspection_recommended=code_inspection_recommended,
    )
    operational_instincts = _filter_operational_instincts_for_recommended_calls(
        operational_instincts,
        recommended_mcp_calls,
    )
    law_block = (
        build_law_projection_block(laws, variant="compact")
        if resolved_profile == "handoff_compact"
        else build_law_context_block(laws)
    )

    blocks = [f"## Project Context for: {task}\n"]
    for block in (
        law_block,
        render_operational_instincts_block(operational_instincts),
        _build_component_context_block(components),
        _build_improvement_context_block(improvements),
        _build_runtime_hint_context_block(runtime_hints),
        _build_memoir_context_block(memoirs),
        _build_task_triage_context_block(task_triage),
        _build_task_context_block(tasks),
        _build_task_capture_context_block(task_capture_candidates),
        _build_docs_context_block(docs_sections),
        _build_promoted_canonical_context_block(promoted_canonicals),
        _build_recommended_mcp_calls_block(recommended_mcp_calls),
        _build_deferred_context_block(deferred_sources),
    ):
        if block:
            blocks.extend([block, ""])

    if not any((components, laws, improvements, runtime_hints, memoirs, tasks, docs_sections)):
        message = "No relevant project knowledge found. Ingest components or build project knowledge first."
    elif not components and laws and not improvements and not runtime_hints and not memoirs and not tasks and not docs_sections:
        message = "No relevant components found. Returned applicable project laws only."
    elif not components:
        message = "No relevant components found. Returned other relevant project knowledge."
    else:
        message = ""
    if promoted_canonicals:
        message = (message + " Included promoted canonicals as fallback knowledge.").strip() if message else "Included promoted canonicals as fallback knowledge."
    if deferred_sources:
        deferred_note = "Deferred background synthesis for: " + ", ".join(deferred_sources) + "."
        message = (message + " " + deferred_note).strip() if message else deferred_note

    return ProjectContextBundle(
        project_id=project_id,
        task=task,
        context="\n".join(blocks).strip(),
        components=[
            {
                "component_id": row.get("component_id"),
                "name": row.get("name"),
                "score": row.get("_score"),
                "key_files": row.get("key_files", []),
            }
            for row in components
        ],
        laws=[law.model_dump() for law in laws],
        improvements=improvements,
        runtime_hints=runtime_hints,
        memoirs=memoirs,
        tasks=tasks,
        promoted_canonicals=promoted_canonicals,
        recommended_mcp_calls=recommended_mcp_calls,
        task_triage=task_triage,
        task_capture_candidates=task_capture_candidates,
        docs_sections=docs_sections,
        operational_instincts=operational_instincts,
        coverage=coverage,
        missing_sources=missing_sources,
        deferred_sources=deferred_sources,
        code_inspection_recommended=code_inspection_recommended,
        message=message,
    )


async def gather_project_knowledge_snapshot(
    *,
    project_id: str,
    qdrant,
    collection: str | None = None,
    ollama,
    max_runtime_hints: int = 10,
    max_memoirs: int = 10,
    max_tasks: int = 10,
    max_improvements: int = 200,
) -> dict[str, Any]:
    if _is_qdrant_service_like(qdrant):
        adapter = qdrant
    else:
        if not collection:
            raise ValueError("Collection is required when passing a raw Qdrant client")
        adapter = _QdrantAdapter(qdrant, collection)
    client = adapter._client
    collection = adapter._collection
    svc = ProjectKnowledgeService(client, ollama)
    await svc.ensure_collection()
    (
        components,
        laws,
        tasks,
        task_entities_count,
        improvements,
        runtime_hints,
        memoirs,
        docs_sections,
    ) = await asyncio.gather(
        svc.list_components(project_id),
        list_project_laws(
            adapter,
            project=project_id,
            status="active",
            include_promoted=True,
            limit=50,
        ),
        _fetch_recent_tasks(adapter, project_id, limit=max_tasks),
        _count_task_entities(adapter, project_id),
        _fetch_improvements(project_id, status=None, limit=max_improvements),
        _fetch_runtime_hints(project_id, limit=max_runtime_hints),
        _fetch_recent_memoirs(client, collection, project_id, limit=max_memoirs),
        _fetch_effective_doc_sections(client, collection, project_id),
    )
    return {
        "project_id": project_id,
        "components": components,
        "laws": [law.model_dump() for law in laws],
        "improvements": improvements,
        "runtime_hints": runtime_hints,
        "memoirs": memoirs,
        "tasks": tasks,
        "task_entities_count": task_entities_count,
        "docs_sections": docs_sections,
    }


def _readiness_coverage(snapshot: dict[str, Any]) -> dict[str, int]:
    return {
        "components": len(snapshot.get("components") or []),
        "laws": len(snapshot.get("laws") or []),
        "improvements": len(snapshot.get("improvements") or []),
        "runtime_hints": len(snapshot.get("runtime_hints") or []),
        "memoirs": len(snapshot.get("memoirs") or []),
        "tasks": int(snapshot.get("task_entities_count") or len(snapshot.get("tasks") or [])),
        "docs_sections": len(snapshot.get("docs_sections") or []),
    }


def _latest_snapshot_info(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    components = snapshot.get("components") or []
    with_snapshot = [item for item in components if item.get("snapshot")]
    if not with_snapshot:
        return None
    snapshots = [item.get("snapshot") or {} for item in with_snapshot]
    commit_shas = [str(item.get("commit_sha") or "").strip() for item in snapshots if str(item.get("commit_sha") or "").strip()]
    source_modes = [str(item.get("source_mode") or "workspace") for item in snapshots]
    repos = [str(item.get("repo") or "").strip() for item in snapshots if str(item.get("repo") or "").strip()]
    branches = [str(item.get("branch") or "").strip() for item in snapshots if str(item.get("branch") or "").strip()]
    return {
        "present": True,
        "source_mode": source_modes[-1] if source_modes else "workspace",
        "repo": repos[-1] if repos else "",
        "branch": branches[-1] if branches else "",
        "commit_sha": commit_shas[-1] if commit_shas else "",
        "component_count": len(with_snapshot),
        "consistent_commit": len(set(commit_shas)) <= 1 if commit_shas else False,
    }


def _readiness_score(coverage: dict[str, int]) -> int:
    score = 0
    if coverage["components"] > 0:
        score += 25
    if coverage["docs_sections"] > 0:
        score += 20
    if coverage["improvements"] > 0:
        score += 10
    if coverage["tasks"] > 0:
        score += 10
    if coverage["laws"] > 0:
        score += 10
    if coverage["runtime_hints"] > 0:
        score += 10
    if coverage["memoirs"] > 0:
        score += 5
    if coverage["components"] > 0 and coverage["docs_sections"] > 0:
        score += 10
    return min(score, 100)


def _readiness_strengths(coverage: dict[str, int]) -> list[str]:
    strengths: list[str] = []
    if coverage["components"] > 0:
        strengths.append("Component knowledge is indexed.")
    if coverage["docs_sections"] > 0:
        strengths.append("Effective documentation projection is available.")
    if coverage["tasks"] > 0 or coverage["improvements"] > 0:
        strengths.append("Project work state is represented in memory.")
    if coverage["laws"] > 0:
        strengths.append("Project governance laws are active.")
    if coverage["runtime_hints"] > 0:
        strengths.append("Confirmed runtime guidance exists.")
    if coverage["memoirs"] > 0:
        strengths.append("Decision memoirs are available for retrieval.")
    return strengths


def _readiness_blocking_gaps(coverage: dict[str, int]) -> list[str]:
    gaps: list[str] = []
    if coverage["components"] == 0 and coverage["docs_sections"] == 0:
        gaps.append("No component knowledge or effective documentation is available yet.")
    if coverage["tasks"] == 0 and coverage["improvements"] == 0:
        gaps.append("No explicit project work state exists yet; create at least one task or improvement.")
    return gaps


def _readiness_actions(coverage: dict[str, int], blocking_gaps: list[str]) -> list[str]:
    actions: list[str] = []
    if coverage["components"] == 0:
        actions.append("Ingest or refresh project components so the system can retrieve component knowledge.")
    if coverage["docs_sections"] == 0:
        actions.append("Run documentation rebuild and sync effective doc sections into memory.")
    if coverage["tasks"] == 0 and coverage["improvements"] == 0:
        actions.append("Create or import initial improvements/tasks so the project has explicit work state.")
    if coverage["laws"] == 0:
        actions.append("Optionally import or define initial project laws for stronger governance and context.")
    if coverage["runtime_hints"] == 0:
        actions.append("Normal for a fresh project: runtime hints will appear after reviewed learning cycles.")
    if coverage["memoirs"] == 0:
        actions.append("Normal early in a project: memoirs will appear after tasks accumulate change history.")
    if not blocking_gaps:
        actions.append("Run a first external-project pilot task and review the resulting context quality.")
    return actions


def _readiness_level(coverage: dict[str, int], blocking_gaps: list[str]) -> tuple[str, bool]:
    if blocking_gaps:
        return "bootstrap_needed", False
    if coverage["laws"] == 0 or coverage["components"] == 0 or coverage["docs_sections"] == 0:
        return "limited_pilot_ready", True
    return "pilot_ready", True


def _build_readiness_instinct_lines(instincts: list[dict[str, Any]]) -> list[str]:
    if not instincts:
        return []
    lines = ["Operational instincts:"]
    lines.extend(f"- [{item['rank']}] {item['instinct_id']}: {item['action']}" for item in instincts)
    return lines


async def assess_project_readiness(
    *,
    project_id: str,
    qdrant,
    ollama,
) -> ProjectReadinessReport:
    snapshot = await gather_project_knowledge_snapshot(
        project_id=project_id,
        qdrant=qdrant,
        ollama=ollama,
        max_runtime_hints=50,
        max_memoirs=20,
        max_tasks=50,
        max_improvements=500,
    )
    coverage = _readiness_coverage(snapshot)
    snapshot_info = _latest_snapshot_info(snapshot)
    blocking_gaps = _readiness_blocking_gaps(coverage)
    recommended_actions = _readiness_actions(coverage, blocking_gaps)
    strengths = _readiness_strengths(coverage)
    if snapshot_info and snapshot_info.get("commit_sha"):
        strengths.append("Knowledge is anchored to an explicit code snapshot.")
    else:
        recommended_actions.append("Attach repo/branch/commit snapshot metadata during ingest or refresh for traceable project knowledge.")
    readiness_level, external_pilot_ready = _readiness_level(coverage, blocking_gaps)
    readiness_score = _readiness_score(coverage)
    code_inspection_recommended = coverage["components"] == 0 and coverage["docs_sections"] == 0
    storage_trust_status = build_storage_trust_report().get("status", "ok")
    operational_instincts = get_active_operational_instincts(
        context_type="project_readiness",
        project_id=project_id,
        storage_trust_status=storage_trust_status,
        code_inspection_recommended=code_inspection_recommended,
        limit=5,
    )
    summary = (
        f"Project '{project_id}' is {readiness_level} "
        f"({readiness_score}/100). "
        f"Knowledge coverage: components={coverage['components']}, docs={coverage['docs_sections']}, "
        f"tasks={coverage['tasks']}, improvements={coverage['improvements']}, "
        f"laws={coverage['laws']}, runtime_hints={coverage['runtime_hints']}, memoirs={coverage['memoirs']}."
    )
    if snapshot_info and snapshot_info.get("commit_sha"):
        summary += f" Snapshot commit: {snapshot_info['commit_sha'][:12]}."
    return ProjectReadinessReport(
        project_id=project_id,
        readiness_level=readiness_level,
        readiness_score=readiness_score,
        external_pilot_ready=external_pilot_ready,
        coverage=coverage,
        blocking_gaps=blocking_gaps,
        recommended_actions=recommended_actions,
        strengths=strengths,
        operational_instincts=operational_instincts,
        snapshot=snapshot_info,
        code_inspection_recommended=code_inspection_recommended,
        summary=summary,
    )


def _bootstrap_step(
    *,
    step_id: str,
    title: str,
    required: bool,
    completed: bool,
    rationale: str,
    action: str,
    tool_hint: str,
) -> dict[str, Any]:
    if completed:
        status = "completed"
    elif required:
        status = "pending"
    else:
        status = "recommended"
    return {
        "step_id": step_id,
        "title": title,
        "required": required,
        "status": status,
        "rationale": rationale,
        "action": action,
        "tool_hint": tool_hint,
    }


async def build_project_bootstrap_checklist(
    *,
    project_id: str,
    qdrant,
    ollama,
) -> ProjectBootstrapChecklist:
    report = await assess_project_readiness(project_id=project_id, qdrant=qdrant, ollama=ollama)
    coverage = report.coverage
    work_state_ready = coverage["tasks"] > 0 or coverage["improvements"] > 0
    snapshot_present = bool((report.snapshot or {}).get("present"))

    steps = [
        _bootstrap_step(
            step_id="snapshot_attached",
            title="Attach explicit repository snapshot metadata",
            required=False,
            completed=snapshot_present,
            rationale="Snapshot-aware knowledge can be traced to repo, branch, and commit instead of an ambiguous workspace state.",
            action="Provide repo, branch, and commit metadata during project ingest or refresh.",
            tool_hint="project/ingest or project/refresh with snapshot",
        ),
        _bootstrap_step(
            step_id="components_indexed",
            title="Index or refresh component knowledge",
            required=True,
            completed=coverage["components"] > 0,
            rationale="Agents need component-level project understanding before falling back to code inspection.",
            action="Ingest the project or refresh indexed components.",
            tool_hint="project/ingest or project/refresh",
        ),
        _bootstrap_step(
            step_id="docs_projected",
            title="Build effective documentation projection",
            required=True,
            completed=coverage["docs_sections"] > 0,
            rationale="A new project should have at least a minimal memory-first documentation projection.",
            action="Rebuild docs and sync effective doc sections into memory.",
            tool_hint="docs/rebuild",
        ),
        _bootstrap_step(
            step_id="work_state_seeded",
            title="Seed explicit project work state",
            required=True,
            completed=work_state_ready,
            rationale="The system needs at least one task or improvement to represent active project work.",
            action="Create or import initial improvements/tasks.",
            tool_hint="improvements/create or project/tasks",
        ),
        _bootstrap_step(
            step_id="laws_initialized",
            title="Initialize project laws",
            required=False,
            completed=coverage["laws"] > 0,
            rationale="Project laws improve governance and retrieval quality but are optional for a first pilot.",
            action="Import markdown laws or create initial laws manually.",
            tool_hint="laws/import-markdown or laws/create",
        ),
        _bootstrap_step(
            step_id="sample_retrieval_validated",
            title="Validate memory-first retrieval on a representative task",
            required=False,
            completed=False,
            rationale="A pilot project should be validated by running enrich-task before starting normal coding work.",
            action="Run enrich-task with a realistic project task and inspect coverage, blockers, and fallback advice.",
            tool_hint="project/enrich-task or MCP enrich_task_with_context",
        ),
    ]
    next_step = next(
        (step["step_id"] for step in steps if step["status"] == "pending"),
        next((step["step_id"] for step in steps if step["status"] == "recommended"), "done"),
    )
    operational_instincts = get_active_operational_instincts(
        context_type="bootstrap_checklist",
        project_id=project_id,
        storage_trust_status=build_storage_trust_report().get("status", "ok"),
        code_inspection_recommended=report.code_inspection_recommended,
        limit=5,
    )
    summary = (
        f"Bootstrap checklist for '{project_id}': {report.readiness_level}. "
        f"Next step: {next_step}."
    )
    return ProjectBootstrapChecklist(
        project_id=project_id,
        readiness_level=report.readiness_level,
        bootstrap_ready=report.external_pilot_ready,
        next_step=next_step,
        steps=steps,
        operational_instincts=operational_instincts,
        summary=summary,
    )
