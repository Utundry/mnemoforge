from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid5, NAMESPACE_URL

from qdrant_client.http import models as qmodels

from app.models.docs import DocsStatus
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.services.embedding_gateway import embed_text
from app.services.memory_store import get_memory_store

DOC_SECTION_CATEGORY = "doc_section"
DOC_SECTION_SOURCE = "docs-projection"
_MAX_DOC_SECTION_CHARS = 9000
_DOC_SECTION_PAYLOAD_CONTENT_PREFIX = "doc_section_ref:"


def doc_section_memory_id(project: str, section_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"mnemoforge:doc_section:{project}:{section_key}")


def _doc_section_payload_ref(memory_id: UUID | str) -> str:
    return f"{_DOC_SECTION_PAYLOAD_CONTENT_PREFIX}{memory_id}"


def _is_doc_section_payload_ref(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_DOC_SECTION_PAYLOAD_CONTENT_PREFIX)


def _doc_section_meta(status: DocsStatus, section_key: str, section_name: str) -> dict:
    return {
        "entity_type": "doc_section",
        "section_key": section_key,
        "section_name": section_name,
        "generated_at": status.generated_at.isoformat(),
        "candidate_available": bool(status.candidate_sections),
        "last_review_action": status.last_review_action,
        "last_reviewed_by": status.last_reviewed_by,
        "last_review_source": status.last_review_source,
        "last_reviewed_at": status.last_reviewed_at.isoformat() if status.last_reviewed_at else None,
        "last_review_reason": status.last_review_reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _projectable_doc_section_content(content: str) -> tuple[str, bool]:
    body = (content or "").strip()
    if len(body) <= _MAX_DOC_SECTION_CHARS:
        return body, False
    clipped = body[:_MAX_DOC_SECTION_CHARS].rstrip()
    return clipped + "\n\n[Truncated for memory-first agent context]", True


def build_doc_section_memory(project: str, section_key: str, status: DocsStatus) -> MemoryCreate | None:
    section = status.sections.get(section_key)
    if not section or not section.content.strip():
        return None
    content, truncated = _projectable_doc_section_content(section.content)
    meta = _doc_section_meta(status, section_key, section.name)
    meta["truncated_for_memory"] = truncated
    return MemoryCreate(
        content=content,
        agent_id="system",
        memory_type=MemoryType.procedural,
        category=DOC_SECTION_CATEGORY,
        importance_score=0.82,
        source=DOC_SECTION_SOURCE,
        tags=[
            "doc_section",
            f"project:{project}",
            f"section:{section_key}",
        ],
        project=project,
        scope="project",
        status="active",
        topic_path=f"docs/{section_key}",
        meta=meta,
    )


async def sync_effective_doc_sections(qdrant, ollama, status: DocsStatus) -> list[str]:
    synced_ids: list[str] = []
    active_ids: list[str] = []
    content_store = get_memory_store()

    for section_key in status.sections.keys():
        memory = build_doc_section_memory(status.project, section_key, status)
        if memory is None:
            continue
        memory_id = doc_section_memory_id(status.project, section_key)
        active_ids.append(str(memory_id))
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "content": _doc_section_payload_ref(memory_id),
            "agent_id": memory.agent_id,
            "memory_type": memory.memory_type.value,
            "category": memory.category,
            "importance_score": memory.importance_score,
            "timestamp": timestamp,
            "source": memory.source,
            "tags": memory.tags,
            "access_count": 0,
            "session_id": None,
            "status": memory.status,
            "meta": memory.meta,
            "decay_rate": memory.effective_decay_rate,
            "pinned": memory.pinned,
            "last_access_ts": None,
            "last_decay_ts": None,
            "related_ids": [],
            "project": memory.project,
            "expires_at": None,
            "topic_path": memory.topic_path,
            "scope": memory.scope,
            "supports": [],
            "canonical_id": None,
        }
        store_metadata = dict(payload)
        store_metadata.pop("content", None)
        await content_store.upsert(
            memory_id=str(memory_id),
            category=DOC_SECTION_CATEGORY,
            content=memory.content,
            metadata=store_metadata,
        )
        vector, embedding_meta = await embed_text(
            memory.content,
            primary=ollama,
            purpose="doc_section",
            fallback_reason="doc_section_embedding_unavailable",
        )
        payload["meta"] = {**(payload.get("meta") or {}), **embedding_meta}
        await qdrant._client.upsert(
            collection_name=qdrant._collection,
            points=[
                qmodels.PointStruct(
                    id=str(memory_id),
                    vector=vector,
                    payload=payload,
                )
            ],
        )
        synced_ids.append(str(memory_id))

    existing, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=DOC_SECTION_CATEGORY)),
            qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=status.project)),
        ]),
        limit=100,
        with_payload=False,
        with_vectors=False,
    )
    stale_ids = [str(point.id) for point in existing if str(point.id) not in active_ids]
    if stale_ids:
        await qdrant._client.delete(
            collection_name=qdrant._collection,
            points_selector=qmodels.PointIdsList(points=stale_ids),
        )
        for stale_id in stale_ids:
            await content_store.delete(stale_id)
    return synced_ids


async def list_doc_sections(qdrant_client, collection: str, project: str, *, limit: int = 50) -> list[dict]:
    results, _ = await qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=DOC_SECTION_CATEGORY)),
            qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
            qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value="active")),
        ]),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    rows = []
    ids = [str(point.id) for point in results if point.payload]
    store_rows = await get_memory_store().get_many(ids)
    for point in results:
        payload = dict(point.payload or {})
        if not payload:
            continue
        point_id = str(point.id)
        if _is_doc_section_payload_ref(payload.get("content")):
            store_row = store_rows.get(point_id)
            if store_row:
                payload["content"] = store_row.get("content") or ""
        rows.append(payload)
    rows.sort(key=lambda payload: str((payload.get("meta") or {}).get("section_key") or ""))
    return rows


async def backfill_legacy_doc_sections_to_store(
    qdrant_client,
    collection: str,
    *,
    limit: int = 500,
    rewrite_qdrant_refs: bool = False,
    dry_run: bool = True,
) -> dict[str, object]:
    store = get_memory_store()
    scanned = 0
    legacy_candidates = 0
    copied_to_sqlite = 0
    already_in_sqlite = 0
    rewritten_qdrant_refs = 0
    already_ref_payload = 0
    failed = 0
    failed_ids: list[str] = []
    offset = None

    while scanned < limit:
        batch_size = min(200, max(limit - scanned, 1))
        results, next_offset = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=DOC_SECTION_CATEGORY)),
            ]),
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not results:
            break
        for point in results:
            if scanned >= limit:
                break
            scanned += 1
            point_id = str(point.id)
            payload = dict(point.payload or {})
            content = payload.get("content")
            try:
                if _is_doc_section_payload_ref(content):
                    already_ref_payload += 1
                    continue
                if not isinstance(content, str) or not content.strip():
                    continue
                legacy_candidates += 1
                exists_in_store = await store.exists(point_id)
                if exists_in_store:
                    already_in_sqlite += 1
                elif not dry_run:
                    metadata = dict(payload)
                    metadata.pop("content", None)
                    await store.upsert(
                        memory_id=point_id,
                        category=DOC_SECTION_CATEGORY,
                        content=content,
                        metadata=metadata,
                    )
                    copied_to_sqlite += 1
                if rewrite_qdrant_refs and not dry_run:
                    await qdrant_client.set_payload(
                        collection_name=collection,
                        payload={"content": _doc_section_payload_ref(point_id)},
                        points=[point.id],
                    )
                    rewritten_qdrant_refs += 1
            except Exception as exc:
                failed += 1
                failed_ids.append(point_id)
                continue
        if next_offset is None:
            break
        offset = next_offset

    return {
        "collection": collection,
        "limit": limit,
        "dry_run": dry_run,
        "rewrite_qdrant_refs": rewrite_qdrant_refs,
        "scanned": scanned,
        "legacy_candidates": legacy_candidates,
        "copied_to_sqlite": copied_to_sqlite,
        "already_in_sqlite": already_in_sqlite,
        "rewritten_qdrant_refs": rewritten_qdrant_refs,
        "already_ref_payload": already_ref_payload,
        "failed": failed,
        "failed_ids": failed_ids,
    }
