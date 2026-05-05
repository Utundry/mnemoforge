from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from qdrant_client.http import models as qmodels

from app.services.embedding_gateway import embed_text
from app.services.memory_store import get_memory_store
from app.services.project_tasks_content import build_task_change_content, build_task_content
from app.services.project_tasks_store import get_project_tasks_store

logger = logging.getLogger(__name__)

HANDOFF_TARGET = "handoff"
MEMORY_TARGET = "memory"
SKILL_TARGET = "skill"
CODE_COMPONENT_TARGET = "code_component"
TASK_MEMOIR_TARGET = "task_memoir"
DOC_SECTION_TARGET = "doc_section"
PROJECT_TASK_TARGET = "project_task"
TASK_CHANGE_TARGET = "task_change"

GENERIC_MEMORY_EXCLUDED_CATEGORIES = frozenset(
    {
        HANDOFF_TARGET,
        SKILL_TARGET,
        CODE_COMPONENT_TARGET,
        TASK_MEMOIR_TARGET,
        DOC_SECTION_TARGET,
    }
)

SUPPORTED_QDRANT_REBUILD_TARGETS = (
    MEMORY_TARGET,
    HANDOFF_TARGET,
    SKILL_TARGET,
    CODE_COMPONENT_TARGET,
    TASK_MEMOIR_TARGET,
    DOC_SECTION_TARGET,
    PROJECT_TASK_TARGET,
    TASK_CHANGE_TARGET,
)

_HANDOFF_PAYLOAD_CONTENT_PREFIX = "handoff_ref:"
_MEMOIR_PAYLOAD_CONTENT_PREFIX = "memoir_ref:"
_DOC_SECTION_PAYLOAD_CONTENT_PREFIX = "doc_section_ref:"


def _handoff_payload_ref(memory_id: str) -> str:
    return f"{_HANDOFF_PAYLOAD_CONTENT_PREFIX}{memory_id}"


def _memoir_payload_ref(memory_id: str) -> str:
    return f"{_MEMOIR_PAYLOAD_CONTENT_PREFIX}{memory_id}"


def _doc_section_payload_ref(memory_id: str) -> str:
    return f"{_DOC_SECTION_PAYLOAD_CONTENT_PREFIX}{memory_id}"


def normalize_rebuild_targets(targets: list[str] | None) -> list[str]:
    requested = [str(target or "").strip() for target in (targets or []) if str(target or "").strip()]
    if not requested:
        return list(SUPPORTED_QDRANT_REBUILD_TARGETS)
    seen: set[str] = set()
    normalized: list[str] = []
    for item in requested:
        if item not in SUPPORTED_QDRANT_REBUILD_TARGETS or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _utc_iso(ts: float | int | None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def is_generic_memory_store_row(row: dict[str, Any]) -> bool:
    metadata = dict(row.get("metadata") or {})
    category = str(metadata.get("category") or "").strip()
    memory_type = str(metadata.get("memory_type") or "").strip()
    content = str(row.get("content") or "")
    memory_id = str(row.get("memory_id") or "").strip()
    if not memory_id or not content or not category or not memory_type:
        return False
    return category not in GENERIC_MEMORY_EXCLUDED_CATEGORIES


async def _iter_handoff_rows(limit: int) -> tuple[list[dict[str, Any]], int]:
    store = get_memory_store()
    rows: list[dict[str, Any]] = []
    scanned = 0
    offset = 0
    batch_size = min(max(limit, 50), 200)

    while len(rows) < limit:
        batch = await store.list_rows(category="memory", limit=batch_size, offset=offset)
        if not batch:
            break
        scanned += len(batch)
        offset += len(batch)
        for row in batch:
            metadata = dict(row.get("metadata") or {})
            if metadata.get("category") != HANDOFF_TARGET:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows, scanned


async def _iter_generic_memory_rows(limit: int) -> tuple[list[dict[str, Any]], int]:
    store = get_memory_store()
    rows: list[dict[str, Any]] = []
    scanned = 0
    offset = 0
    batch_size = min(max(limit, 50), 200)

    while len(rows) < limit:
        batch = await store.list_rows(category="memory", limit=batch_size, offset=offset)
        if not batch:
            break
        scanned += len(batch)
        offset += len(batch)
        for row in batch:
            if not is_generic_memory_store_row(row):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows, scanned


async def _rows_for_store_target(
    *,
    store,
    store_category: str,
    limit: int,
    record_ids: list[str] | None = None,
    metadata_category: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    if record_ids:
        rows = list((await store.get_many(record_ids)).values())
        if store_category:
            rows = [
                row for row in rows
                if row.get("category") == store_category
                or dict(row.get("metadata") or {}).get("category") == (metadata_category or store_category)
            ]
        elif metadata_category:
            rows = [
                row for row in rows
                if dict(row.get("metadata") or {}).get("category") == metadata_category
            ]
        return rows[:limit], len(rows)
    rows = await store.list_rows(category=store_category, limit=limit, offset=0)
    return rows, len(rows)


def _handoff_point_from_store_row(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("memory_id") or "").strip()
    content = str(row.get("content") or "")
    metadata = dict(row.get("metadata") or {})
    if not memory_id or not content or metadata.get("category") != HANDOFF_TARGET:
        return None
    payload = dict(metadata)
    payload["content"] = _handoff_payload_ref(memory_id)
    payload["meta"] = {}
    return memory_id, content, payload


def _generic_memory_point_from_store_row(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("memory_id") or "").strip()
    content = str(row.get("content") or "")
    metadata = dict(row.get("metadata") or {})
    if not memory_id or not content or not metadata.get("category"):
        return None
    payload = dict(metadata)
    payload["content"] = content
    return memory_id, content, payload


def _memoir_point_from_store_row(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("memory_id") or "").strip()
    content = str(row.get("content") or "")
    metadata = dict(row.get("metadata") or {})
    if not memory_id or not content:
        return None
    payload = dict(metadata)
    payload["content"] = _memoir_payload_ref(memory_id)
    return memory_id, content[:500], payload


def _doc_section_point_from_store_row(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("memory_id") or "").strip()
    content = str(row.get("content") or "")
    metadata = dict(row.get("metadata") or {})
    if not memory_id or not content:
        return None
    payload = dict(metadata)
    payload["content"] = _doc_section_payload_ref(memory_id)
    return memory_id, content, payload


def _skill_point_from_store_row(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("memory_id") or "").strip()
    content = str(row.get("content") or "")
    metadata = dict(row.get("metadata") or {})
    if not memory_id or not content:
        return None
    skill_name = str(metadata.get("skill_name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    domain_tags = list(metadata.get("domain_tags") or [])
    payload = {
        "content": content,
        "agent_id": str(metadata.get("agent_id") or "shared"),
        "memory_type": str(metadata.get("memory_type") or "context"),
        "category": "skill",
        "importance_score": float(metadata.get("importance_score") or 0.5),
        "timestamp": str(metadata.get("timestamp") or _utc_iso(row.get("created_at"))),
        "source": str(metadata.get("source") or f"skill-publish:{skill_name or memory_id}"),
        "tags": list(metadata.get("tags") or []),
        "access_count": 0,
        "session_id": None,
        "status": str(metadata.get("review_status") or "").strip() or None,
        "decay_rate": 1.0,
        "pinned": bool(metadata.get("pinned", False)),
        "last_access_ts": None,
        "last_decay_ts": None,
        "related_ids": [],
        "project": None,
        "expires_at": None,
        "topic_path": None,
        "scope": "project",
        "supports": [],
        "canonical_id": None,
        "skill_name": skill_name or "unknown",
        "skill_description": description or content[:100],
        "platform": str(metadata.get("platform") or "claude"),
        "domain_tags": domain_tags,
        "suppressed": bool(metadata.get("suppressed", False)),
        "pinned": bool(metadata.get("pinned", False)),
        "review_status": metadata.get("review_status"),
        "auto_generated": bool(metadata.get("auto_generated", False)),
    }
    reference_url = metadata.get("reference_url")
    if reference_url:
        payload["reference_url"] = reference_url
    embed_text = f"{payload['skill_name']} {payload['skill_description']} {' '.join(domain_tags)}"
    return memory_id, embed_text.strip() or content[:1200], payload


def _code_component_point_from_store_row(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("memory_id") or "").strip()
    content = str(row.get("content") or "")
    metadata = dict(row.get("metadata") or {})
    rel_path = str(metadata.get("code_path") or "").strip()
    symbol = str(metadata.get("code_symbol") or "").strip()
    chunk_type = str(metadata.get("code_chunk_type") or "").strip()
    language = str(metadata.get("code_language") or "").strip()
    if not memory_id or not content or not rel_path or not language:
        return None
    imports = list(metadata.get("code_imports") or [])
    payload = {
        "content": content,
        "agent_id": str(metadata.get("agent_id") or "code-search"),
        "memory_type": str(metadata.get("memory_type") or "context"),
        "category": "code_component",
        "importance_score": float(metadata.get("importance_score") or 0.45),
        "timestamp": str(metadata.get("timestamp") or _utc_iso(row.get("created_at"))),
        "source": str(metadata.get("source") or f"code-index:{rel_path}"),
        "tags": list(metadata.get("tags") or []),
        "access_count": 0,
        "session_id": metadata.get("session_id"),
        "status": None,
        "meta": {},
        "decay_rate": float(metadata.get("decay_rate") or 0.0),
        "pinned": False,
        "last_access_ts": None,
        "last_decay_ts": None,
        "related_ids": [],
        "project": None,
        "expires_at": None,
        "topic_path": None,
        "scope": "project",
        "supports": [],
        "canonical_id": None,
        "code_path": rel_path,
        "code_symbol": symbol,
        "code_chunk_type": chunk_type,
        "code_language": language,
    }
    if imports:
        payload["code_imports"] = imports
    imports_text = " ".join(imports[:20]) if imports else ""
    embed_text = (
        f"{language} {chunk_type} {symbol} {rel_path}"
        + (f" imports:{imports_text}" if imports_text else "")
        + f"\n{content[:1200]}"
    )
    return memory_id, embed_text, payload


def _project_task_payload(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("id") or "").strip()
    project = str(row.get("project") or "").strip()
    task_id = str(row.get("task_id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not memory_id or not project or not task_id or not title:
        return None
    description = str(row.get("description") or "")
    content = build_task_content(title, description)
    payload = {
        "content": content,
        "agent_id": str(row.get("agent_id") or "system"),
        "memory_type": "task",
        "category": "task",
        "importance_score": 0.8,
        "timestamp": _utc_iso(row.get("created_at")),
        "source": str(row.get("source") or "project_task"),
        "tags": list(row.get("tags") or []),
        "access_count": 0,
        "session_id": None,
        "status": str(row.get("status") or "planning"),
        "meta": {
            "entity_type": "project_task",
            "task_id": task_id,
            "title": title,
            "description": description,
            "created_at": _utc_iso(row.get("created_at")),
            "updated_at": _utc_iso(row.get("updated_at")),
            "linked_improvement_id": row.get("linked_improvement_id"),
        },
        "decay_rate": 1.0,
        "pinned": False,
        "last_access_ts": None,
        "last_decay_ts": None,
        "related_ids": [],
        "project": project,
        "expires_at": None,
        "topic_path": row.get("topic_path"),
        "scope": "project",
        "supports": [],
        "canonical_id": None,
    }
    return memory_id, content, payload


def _task_change_payload(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    memory_id = str(row.get("id") or "").strip()
    project = str(row.get("project") or "").strip()
    task_id = str(row.get("task_id") or "").strip()
    change_type = str(row.get("change_type") or "").strip()
    content_text = str(row.get("content") or "").strip()
    if not memory_id or not project or not task_id or not change_type or not content_text:
        return None
    why = str(row.get("why") or "")
    content = build_task_change_content(change_type, content_text, why)
    payload = {
        "content": content,
        "agent_id": str(row.get("agent_id") or "system"),
        "memory_type": "experience",
        "category": "task_change",
        "importance_score": 0.72,
        "timestamp": _utc_iso(row.get("created_at")),
        "source": str(row.get("source") or "project_task_change"),
        "tags": list(row.get("tags") or []),
        "access_count": 0,
        "session_id": None,
        "status": None,
        "meta": {
            "entity_type": "task_change",
            "task_id": task_id,
            "change_type": change_type,
            "why": why,
            "created_at": _utc_iso(row.get("created_at")),
        },
        "decay_rate": 1.0,
        "pinned": False,
        "last_access_ts": None,
        "last_decay_ts": None,
        "related_ids": [],
        "project": project,
        "expires_at": None,
        "topic_path": None,
        "scope": "project",
        "supports": [],
        "canonical_id": None,
    }
    return memory_id, content, payload


async def _upsert_rebuilt_points(
    *,
    qdrant,
    ollama,
    points: list[tuple[str, str, dict[str, Any]]],
    dry_run: bool,
) -> tuple[int, list[str]]:
    if dry_run or not points:
        return 0, []
    await qdrant.ensure_collection()
    batch: list[qmodels.PointStruct] = []
    failed_ids: list[str] = []
    upserted = 0

    for memory_id, embed_text, payload in points:
        try:
            vector, embedding_meta = await embed_text(
                embed_text,
                primary=ollama,
                purpose="qdrant_rebuild",
                fallback_reason="qdrant_rebuild_embedding_unavailable",
            )
            payload["meta"] = {**(payload.get("meta") or {}), **embedding_meta}
            batch.append(qmodels.PointStruct(id=memory_id, vector=vector, payload=payload))
            if len(batch) >= 32:
                await qdrant._client.upsert(collection_name=qdrant._collection, points=batch)
                upserted += len(batch)
                batch = []
        except Exception:
            logger.exception("Failed to rebuild Qdrant point %s", memory_id)
            failed_ids.append(memory_id)
    if batch:
        await qdrant._client.upsert(collection_name=qdrant._collection, points=batch)
        upserted += len(batch)
    return upserted, failed_ids


async def reindex_sqlite_backed_qdrant(
    *,
    qdrant,
    ollama,
    targets: list[str] | None = None,
    limit: int = 500,
    dry_run: bool = True,
    record_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected_targets = normalize_rebuild_targets(targets)
    store = get_memory_store()
    task_store = get_project_tasks_store()
    report: dict[str, Any] = {
        "collection": getattr(qdrant, "_collection", ""),
        "limit": limit,
        "limit_mode": "per_target",
        "dry_run": dry_run,
        "targets": selected_targets,
        "supported_targets": list(SUPPORTED_QDRANT_REBUILD_TARGETS),
        "record_ids": list(record_ids or []),
        "scanned": 0,
        "planned_upserts": 0,
        "upserted": 0,
        "upserted_ids": [],
        "failed": 0,
        "failed_ids": [],
        "by_target": {},
    }

    for target in selected_targets:
        points: list[tuple[str, str, dict[str, Any]]] = []
        scanned = 0

        if target == HANDOFF_TARGET:
            if record_ids:
                rows, scanned = await _rows_for_store_target(
                    store=store,
                    store_category="memory",
                    metadata_category=HANDOFF_TARGET,
                    limit=limit,
                    record_ids=record_ids,
                )
            else:
                rows, scanned = await _iter_handoff_rows(limit)
            for row in rows:
                point = _handoff_point_from_store_row(row)
                if point is not None:
                    points.append(point)
        elif target == MEMORY_TARGET:
            if record_ids:
                rows, scanned = await _rows_for_store_target(
                    store=store,
                    store_category="memory",
                    limit=limit,
                    record_ids=record_ids,
                )
            else:
                rows, scanned = await _iter_generic_memory_rows(limit)
            for row in rows:
                point = _generic_memory_point_from_store_row(row)
                if point is not None:
                    points.append(point)
        elif target == SKILL_TARGET:
            rows, scanned = await _rows_for_store_target(
                store=store,
                store_category=SKILL_TARGET,
                limit=limit,
                record_ids=record_ids,
            )
            for row in rows:
                point = _skill_point_from_store_row(row)
                if point is not None:
                    points.append(point)
        elif target == CODE_COMPONENT_TARGET:
            rows, scanned = await _rows_for_store_target(
                store=store,
                store_category=CODE_COMPONENT_TARGET,
                limit=limit,
                record_ids=record_ids,
            )
            for row in rows:
                point = _code_component_point_from_store_row(row)
                if point is not None:
                    points.append(point)
        elif target == TASK_MEMOIR_TARGET:
            rows, scanned = await _rows_for_store_target(
                store=store,
                store_category=TASK_MEMOIR_TARGET,
                limit=limit,
                record_ids=record_ids,
            )
            for row in rows:
                point = _memoir_point_from_store_row(row)
                if point is not None:
                    points.append(point)
        elif target == DOC_SECTION_TARGET:
            rows, scanned = await _rows_for_store_target(
                store=store,
                store_category=DOC_SECTION_TARGET,
                limit=limit,
                record_ids=record_ids,
            )
            for row in rows:
                point = _doc_section_point_from_store_row(row)
                if point is not None:
                    points.append(point)
        elif target == PROJECT_TASK_TARGET:
            rows = task_store.list_tasks(project=None, status=None, limit=limit)
            scanned = len(rows)
            for row in rows:
                point = _project_task_payload(row)
                if point is not None:
                    points.append(point)
        elif target == TASK_CHANGE_TARGET:
            rows = task_store.list_changes(project=None, task_id=None, limit=limit)
            scanned = len(rows)
            for row in rows:
                point = _task_change_payload(row)
                if point is not None:
                    points.append(point)
        else:
            continue

        planned_upserts = len(points)
        upserted, failed_ids = await _upsert_rebuilt_points(
            qdrant=qdrant,
            ollama=ollama,
            points=points,
            dry_run=dry_run,
        )
        failed = len(failed_ids)

        report["scanned"] += scanned
        report["planned_upserts"] += planned_upserts
        report["upserted"] += upserted
        successful_ids = [memory_id for memory_id, _, _ in points if memory_id not in failed_ids]
        if not dry_run:
            report["upserted_ids"].extend(successful_ids)
        report["failed"] += failed
        report["failed_ids"].extend(failed_ids)
        report["by_target"][target] = {
            "scanned": scanned,
            "planned_upserts": planned_upserts,
            "upserted": upserted,
            "upserted_ids": successful_ids if not dry_run else [],
            "failed": failed,
            "failed_ids": failed_ids,
            "truncated_by_limit": planned_upserts >= limit,
        }

    return report


def register_qdrant_reindex_job_handler(queue) -> None:
    async def _qdrant_reindex_handler(payload: dict[str, Any]) -> dict[str, Any]:
        from app.dependencies import get_ollama, get_qdrant

        report = await reindex_sqlite_backed_qdrant(
            qdrant=get_qdrant(),
            ollama=get_ollama(),
            targets=[str(item) for item in (payload.get("targets") or []) if item],
            limit=max(1, int(payload.get("limit", 100))),
            dry_run=bool(payload.get("dry_run", False)),
            record_ids=[str(item) for item in (payload.get("record_ids") or []) if item],
        )
        return report

    queue.register("qdrant_reindex_from_sqlite", _qdrant_reindex_handler)
