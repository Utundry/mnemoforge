from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from qdrant_client.http import models as qmodels

from app.models.enums import MemoryType
from app.models.law import ProjectLawCandidate, ProjectLawConfirmRequest, ProjectLawCreate, ProjectLawRecord, ProjectLawUpdate
from app.models.memory import MemoryCreate, MemoryUpdate
from app.services.governed_artifact import apply_candidate_fields, build_candidate_revision
from app.services.qdrant_service import _point_to_record

LAW_CATEGORY = "law"
LAW_SOURCE = "project-law"
PROMOTED_SCOPES = {"family", "domain", "principle", "meta"}
CONFIRMED_STATUSES = {"user_confirmed", "active"}
MATERIAL_FIELDS = {"title", "statement", "rationale", "evidence", "scope", "project", "topic_path"}
LAW_CANDIDATE_FIELDS = ("title", "statement", "rationale", "evidence", "version", "scope", "project", "topic_path")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def build_law_content(*, title: str, statement: str, rationale: str, evidence: list[str]) -> str:
    lines = [f"Law: {title.strip()}", statement.strip()]
    if rationale.strip():
        lines.append(f"Rationale: {rationale.strip()}")
    if evidence:
        lines.append("Evidence:")
        lines.extend(f"- {item}" for item in _unique(evidence))
    return "\n\n".join(lines)


def build_law_context_block(laws: list[ProjectLawRecord]) -> str:
    if not laws:
        return ""
    lines = ["## Applicable Project Laws", ""]
    for law in laws:
        locality = "project-local" if law.is_project_local else law.scope
        lines.append(f"- [{locality}] {law.title}: {law.statement}")
        if law.rationale:
            lines.append(f"  Why: {law.rationale}")
    return "\n".join(lines)


def _law_meta_from_create(body: ProjectLawCreate) -> dict:
    now = _utcnow_iso()
    confirmed_by = (body.confirmed_by or "").strip() or None
    confirmed_at = body.confirmed_at.isoformat() if body.confirmed_at else (now if body.status in CONFIRMED_STATUSES and confirmed_by else None)
    return {
        "entity_type": "project_law",
        "title": body.title.strip(),
        "statement": body.statement.strip(),
        "rationale": body.rationale.strip(),
        "evidence": _unique(body.evidence),
        "version": body.version,
        "supersedes": _unique(body.supersedes),
        "supported_by": _unique(body.supported_by),
        "created_at": now,
        "updated_at": now,
        "status_reason": "",
        "confirmed_by": confirmed_by,
        "confirmation_source": (body.confirmation_source or "").strip() or ("direct_user_create" if confirmed_by else None),
        "confirmed_at": confirmed_at,
    }


def _has_confirmation(meta: dict) -> bool:
    return bool((meta.get("confirmed_by") or "").strip() and meta.get("confirmed_at"))


def _validate_target_status(status: str, meta: dict) -> None:
    if status in CONFIRMED_STATUSES and not _has_confirmation(meta):
        raise ValueError("Active or user-confirmed laws require explicit confirmation metadata")


def _law_tags(*, status: str, scope: str, project: Optional[str], extra_tags: list[str]) -> list[str]:
    return _unique(
        [
            tag for tag in extra_tags
            if not str(tag).startswith("law_scope:")
            and not str(tag).startswith("law_status:")
            and not str(tag).startswith("project:")
        ]
        + ["law", f"law_scope:{scope}", f"law_status:{status}"]
        + ([f"project:{project}"] if project else [])
    )


def _candidate_from_meta(meta: dict) -> Optional[ProjectLawCandidate]:
    raw = meta.get("candidate_revision")
    if not isinstance(raw, dict):
        return None
    try:
        return ProjectLawCandidate(**raw)
    except Exception:
        return None


def _current_snapshot(record, meta: dict) -> dict:
    return {
        "title": str(meta.get("title") or record.content.splitlines()[0].replace("Law: ", "", 1)[:256]),
        "statement": str(meta.get("statement") or record.content),
        "rationale": str(meta.get("rationale") or ""),
        "evidence": [str(x) for x in meta.get("evidence", []) if str(x).strip()],
        "version": str(meta.get("version") or "1.0"),
        "scope": record.scope or "project",
        "project": record.project,
        "topic_path": record.topic_path,
    }


def _law_record_from_memory(record, *, requested_project: Optional[str] = None) -> ProjectLawRecord:
    meta = dict(record.meta or {})
    created_raw = meta.get("created_at") or record.timestamp.isoformat()
    updated_raw = meta.get("updated_at") or created_raw
    confirmed_raw = meta.get("confirmed_at")
    status_action_raw = meta.get("last_status_action_at")
    project = record.project
    scope = record.scope or "project"
    candidate = _candidate_from_meta(meta)
    return ProjectLawRecord(
        id=str(record.id),
        project=project,
        scope=scope,
        status=record.status or "active",
        title=str(meta.get("title") or record.content.splitlines()[0].replace("Law: ", "", 1)[:256]),
        statement=str(meta.get("statement") or record.content),
        rationale=str(meta.get("rationale") or ""),
        evidence=[str(x) for x in meta.get("evidence", []) if str(x).strip()],
        version=str(meta.get("version") or "1.0"),
        supersedes=[str(x) for x in meta.get("supersedes", []) if str(x).strip()],
        supported_by=[str(x) for x in meta.get("supported_by", []) if str(x).strip()],
        tags=list(record.tags or []),
        topic_path=record.topic_path,
        source=record.source,
        created_at=datetime.fromisoformat(created_raw),
        updated_at=datetime.fromisoformat(updated_raw),
        memory_id=str(record.id),
        canonical_id=record.canonical_id,
        is_project_local=bool(project and requested_project and project == requested_project and scope == "project"),
        confirmed_by=meta.get("confirmed_by"),
        confirmation_source=meta.get("confirmation_source"),
        confirmed_at=datetime.fromisoformat(confirmed_raw) if confirmed_raw else None,
        status_reason=str(meta.get("status_reason") or ""),
        last_status_action=meta.get("last_status_action"),
        last_status_acted_by=meta.get("last_status_acted_by"),
        last_status_action_source=meta.get("last_status_action_source"),
        last_status_action_at=datetime.fromisoformat(status_action_raw) if status_action_raw else None,
        last_status_action_reason=meta.get("last_status_action_reason"),
        candidate_revision=candidate,
    )


async def create_project_law(qdrant, ollama, body: ProjectLawCreate) -> ProjectLawRecord:
    meta = _law_meta_from_create(body)
    _validate_target_status(body.status, meta)
    content = build_law_content(
        title=body.title,
        statement=body.statement,
        rationale=body.rationale,
        evidence=body.evidence,
    )
    tags = _law_tags(status=body.status, scope=body.scope, project=body.project, extra_tags=list(body.tags or []))
    memory = MemoryCreate(
        content=content,
        agent_id=body.agent_id,
        memory_type=MemoryType.procedural,
        category=LAW_CATEGORY,
        importance_score=0.9 if body.status in CONFIRMED_STATUSES else 0.75,
        source=LAW_SOURCE,
        tags=tags,
        project=body.project,
        scope=body.scope,
        topic_path=body.topic_path,
        status=body.status,
        meta=meta,
    )
    vector = await ollama.embed(memory.content)
    memory_id = await qdrant.insert(memory, vector)
    record = await qdrant.get(memory_id)
    return _law_record_from_memory(record, requested_project=body.project)


async def get_project_law(qdrant, law_id: str) -> ProjectLawRecord:
    record = await qdrant.get(law_id)
    if record.category != LAW_CATEGORY or (record.meta or {}).get("entity_type") != "project_law":
        raise ValueError("Law not found")
    return _law_record_from_memory(record, requested_project=record.project)


async def update_project_law(qdrant, ollama, law_id: str, body: ProjectLawUpdate) -> ProjectLawRecord:
    record = await qdrant.get(law_id)
    if record.category != LAW_CATEGORY or (record.meta or {}).get("entity_type") != "project_law":
        raise ValueError("Law not found")

    meta = dict(record.meta or {})
    title = body.title.strip() if body.title is not None else str(meta.get("title") or "")
    statement = body.statement.strip() if body.statement is not None else str(meta.get("statement") or record.content)
    rationale = body.rationale.strip() if body.rationale is not None else str(meta.get("rationale") or "")
    evidence = _unique(body.evidence) if body.evidence is not None else [str(x) for x in meta.get("evidence", []) if str(x).strip()]

    next_version = body.version if body.version is not None else str(meta.get("version") or "1.0")
    next_supersedes = _unique(body.supersedes) if body.supersedes is not None else [str(x) for x in meta.get("supersedes", []) if str(x).strip()]
    next_supported_by = _unique(body.supported_by) if body.supported_by is not None else [str(x) for x in meta.get("supported_by", []) if str(x).strip()]
    content = build_law_content(title=title, statement=statement, rationale=rationale, evidence=evidence)
    project = body.project if body.project is not None else record.project
    scope = body.scope if body.scope is not None else record.scope
    current_status = record.status or "proposed"
    material_change = any(getattr(body, field) is not None for field in MATERIAL_FIELDS)
    next_status = current_status
    tags = _law_tags(status=next_status, scope=scope, project=project, extra_tags=list(body.tags if body.tags is not None else record.tags or []))

    if material_change and current_status in CONFIRMED_STATUSES:
        base = _candidate_from_meta(meta)
        candidate_base = base.model_dump() if base is not None else _current_snapshot(record, meta)
        candidate = build_candidate_revision(
            base=candidate_base,
            updates={
                "title": body.title.strip() if body.title is not None else None,
                "statement": body.statement.strip() if body.statement is not None else None,
                "rationale": body.rationale.strip() if body.rationale is not None else None,
                "evidence": _unique(body.evidence) if body.evidence is not None else None,
                "version": next_version if body.version is not None else None,
                "scope": body.scope,
                "project": body.project,
                "topic_path": body.topic_path,
            },
            fields=LAW_CANDIDATE_FIELDS,
            proposed_at=_utcnow_iso(),
        )
        meta["candidate_revision"] = candidate
        meta["supersedes"] = next_supersedes
        meta["supported_by"] = next_supported_by
        meta["status_reason"] = "Candidate revision pending explicit confirmation"
        meta["updated_at"] = _utcnow_iso()
        updated = await qdrant.update(
            law_id,
            MemoryUpdate(
                tags=tags,
                meta=meta,
            ),
        )
        return _law_record_from_memory(updated, requested_project=record.project)

    _validate_target_status(next_status, meta)
    meta.update({
        "title": title,
        "statement": statement,
        "rationale": rationale,
        "evidence": evidence,
        "version": next_version,
        "supersedes": next_supersedes,
        "supported_by": next_supported_by,
        "updated_at": _utcnow_iso(),
    })
    updated = await qdrant.update(
        law_id,
        MemoryUpdate(
            content=content,
            tags=tags,
            project=project,
            scope=scope,
            topic_path=body.topic_path if body.topic_path is not None else record.topic_path,
            status=next_status,
            meta=meta,
        ),
        new_vector=await ollama.embed(content),
    )
    return _law_record_from_memory(updated, requested_project=project)


async def update_project_law_status(
    qdrant,
    law_id: str,
    *,
    status: str,
    reason: str = "",
    acted_by: str = "user",
    action_source: str = "inline_user_approval",
) -> ProjectLawRecord:
    record = await qdrant.get(law_id)
    if record.category != LAW_CATEGORY or (record.meta or {}).get("entity_type") != "project_law":
        raise ValueError("Law not found")
    meta = dict(record.meta or {})
    now = _utcnow_iso()
    meta["status_reason"] = reason.strip()
    meta["updated_at"] = now
    meta["last_status_action"] = f"set_status:{status}"
    meta["last_status_acted_by"] = acted_by.strip() or "user"
    meta["last_status_action_source"] = action_source.strip() or "inline_user_approval"
    meta["last_status_action_at"] = now
    meta["last_status_action_reason"] = reason.strip() or None
    _validate_target_status(status, meta)
    tags = _law_tags(
        status=status,
        scope=record.scope or "project",
        project=record.project,
        extra_tags=list(record.tags or []),
    )
    updated = await qdrant.update(
        law_id,
        MemoryUpdate(status=status, meta=meta, tags=tags),
    )
    return _law_record_from_memory(updated, requested_project=record.project)


async def confirm_project_law(qdrant, ollama, law_id: str, body: ProjectLawConfirmRequest) -> ProjectLawRecord:
    record = await qdrant.get(law_id)
    if record.category != LAW_CATEGORY or (record.meta or {}).get("entity_type") != "project_law":
        raise ValueError("Law not found")
    meta = dict(record.meta or {})
    status = "active" if body.activate else "user_confirmed"
    now = _utcnow_iso()
    candidate = _candidate_from_meta(meta)
    meta.update({
        "confirmed_by": body.confirmed_by.strip(),
        "confirmation_source": body.confirmation_source.strip(),
        "confirmed_at": now,
        "status_reason": body.reason.strip(),
        "updated_at": now,
    })
    if candidate is not None:
        applied = apply_candidate_fields(
            effective=_current_snapshot(record, meta),
            candidate=candidate.model_dump(),
            fields=LAW_CANDIDATE_FIELDS,
        )
        meta.update({
            "title": applied["title"],
            "statement": applied["statement"],
            "rationale": applied["rationale"],
            "evidence": applied["evidence"],
            "version": applied["version"],
            "candidate_revision": None,
        })
        scope = applied["scope"]
        project = applied["project"]
        topic_path = applied["topic_path"]
        content = build_law_content(
            title=applied["title"],
            statement=applied["statement"],
            rationale=applied["rationale"],
            evidence=applied["evidence"],
        )
        tags = _law_tags(
            status=status,
            scope=scope,
            project=project,
            extra_tags=list(record.tags or []),
        )
        updated = await qdrant.update(
            law_id,
            MemoryUpdate(
                content=content,
                project=project,
                scope=scope,
                topic_path=topic_path,
                status=status,
                meta=meta,
                tags=tags,
            ),
            new_vector=await ollama.embed(content),
        )
        return _law_record_from_memory(updated, requested_project=project)

    tags = _law_tags(status=status, scope=record.scope or "project", project=record.project, extra_tags=list(record.tags or []))
    updated = await qdrant.update(law_id, MemoryUpdate(status=status, meta=meta, tags=tags))
    return _law_record_from_memory(updated, requested_project=record.project)


async def list_project_laws(
    qdrant,
    *,
    project: Optional[str],
    status: str = "active",
    scope: Optional[str] = None,
    include_promoted: bool = True,
    limit: int = 50,
) -> list[ProjectLawRecord]:
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=LAW_CATEGORY)),
    ]
    if status != "all":
        must.append(qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value=status)))
    if scope:
        must.append(qmodels.FieldCondition(key="scope", match=qmodels.MatchValue(value=scope)))

    points, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=max(limit * 4, 100),
        with_payload=True,
        with_vectors=False,
    )

    laws: list[ProjectLawRecord] = []
    for point in points:
        record = _point_to_record(point)
        if (record.meta or {}).get("entity_type") != "project_law":
            continue
        if project:
            is_local = record.project == project
            is_promoted = include_promoted and (record.project in {None, ""}) and record.scope in PROMOTED_SCOPES
            if not is_local and not is_promoted:
                continue
        law = _law_record_from_memory(record, requested_project=project)
        laws.append(law)

    scope_rank = {"project": 0, "family": 1, "domain": 2, "principle": 3, "meta": 4}
    laws.sort(key=lambda item: (scope_rank.get(item.scope, 99), item.title.casefold(), item.created_at))
    return laws[:limit]
