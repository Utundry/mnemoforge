"""
Task Memoir Service.

Generates a structured retrospective for a completed task:
  1. Fetch original task memory by ID
  2. Fetch all task_change memories tagged with task_id:{uuid}
  3. GLM synthesizes a memoir (with deterministic fallback)
  4. Store as category=task_memoir in Qdrant

Usage by agents — during task work, record changes:
  memory_store(
      content="[change] TTL → event-driven\n[reason] no point rebuilding if nothing changed\n[decision] invalidate on events only",
      category="task_change",
      tags=["task_id:{uuid}"],
  )

Then on resolve, call generate_and_store_memoir(task_id, ...).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings
from app.services.embedding_gateway import embed_text
from app.services.memory_store import get_memory_store

logger = logging.getLogger(__name__)
_MEMOIR_PAYLOAD_CONTENT_PREFIX = "memoir_ref:"


def _memoir_payload_ref(memory_id: UUID | str) -> str:
    return f"{_MEMOIR_PAYLOAD_CONTENT_PREFIX}{memory_id}"


def _is_memoir_payload_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_MEMOIR_PAYLOAD_CONTENT_PREFIX)


def _payload_created_at(payload: dict[str, Any]) -> float:
    ts_raw = payload.get("timestamp")
    if not ts_raw:
        return time.time()
    try:
        return datetime.fromisoformat(str(ts_raw)).timestamp()
    except Exception:
        return time.time()


def _store_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(payload)
    metadata.pop("content", None)
    return metadata


async def hydrate_memoir_payload_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not entries:
        return entries
    ids = [str(item.get("id") or "") for item in entries if str(item.get("id") or "").strip()]
    if not ids:
        return entries
    rows = await get_memory_store().get_many(ids)
    for entry in entries:
        memory_id = str(entry.get("id") or "").strip()
        payload = dict(entry.get("payload") or {})
        if not memory_id or not payload:
            continue
        store_row = rows.get(memory_id)
        if not store_row:
            continue
        if payload.get("category") != "task_memoir":
            continue
        if _is_memoir_payload_ref(payload.get("content")):
            payload["content"] = store_row.get("content") or ""
        metadata = dict(store_row.get("metadata") or {})
        if metadata:
            payload.setdefault("meta", metadata.get("meta") or {})
            for key, value in metadata.items():
                payload.setdefault(key, value)
        entry["payload"] = payload
    return entries


def memoir_quality_status(task: Optional[dict], changes: list[dict], content: str) -> str:
    body = (content or "").strip()
    has_task = bool(task and (task.get("content") or "").strip())
    has_changes = bool(changes)
    weak_markers = (
        "Unknown task",
        "No description available.",
        "_No changes recorded._",
    )
    if any(marker in body for marker in weak_markers):
        return "weak"
    if has_task and has_changes:
        return "grounded"
    if has_task or has_changes:
        return "partial"
    return "weak"


def memoir_generation_preconditions(task: Optional[dict], changes: list[dict]) -> dict[str, Any]:
    reasons: list[str] = []
    task_status = str((task or {}).get("status") or "").strip()
    has_task = bool(task and (task.get("content") or "").strip())
    has_changes = bool(changes)

    if not has_task:
        reasons.append("missing_task")
    if not has_changes:
        reasons.append("missing_changes")

    if not reasons and task_status and task_status not in {"done", "resolved"}:
        reasons.append("task_not_done")

    ready = has_task and has_changes
    return {
        "ready": ready,
        "reasons": reasons,
        "task_status": task_status,
        "change_count": len(changes),
        "task_present": has_task,
    }


async def _fetch_task(
    task_id: str,
    qdrant_client: AsyncQdrantClient,
    collection: str,
) -> Optional[dict]:
    """Fetch canonical task memory by external task_id, with legacy UUID fallback."""
    try:
        results, _ = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value="task"),
                ),
                qmodels.FieldCondition(
                    key="tags",
                    match=qmodels.MatchValue(value=f"task_id:{task_id}"),
                ),
            ]),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        return results[0].payload if results else None
    except Exception as e:
        logger.warning("Failed to fetch task by external task_id %s: %s", task_id, e)

    try:
        results = await qdrant_client.retrieve(
            collection_name=collection,
            ids=[task_id],
            with_payload=True,
            with_vectors=False,
        )
        return results[0].payload if results else None
    except Exception as e:
        logger.warning("Failed legacy task lookup %s: %s", task_id, e)
        return None


async def _fetch_task_changes(
    task_id: str,
    qdrant_client: AsyncQdrantClient,
    collection: str,
) -> list[dict]:
    """Fetch all task_change memories tagged with task_id:{uuid}."""
    try:
        results, _ = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value="task_change"),
                ),
                qmodels.FieldCondition(
                    key="tags",
                    match=qmodels.MatchValue(value=f"task_id:{task_id}"),
                ),
            ]),
            limit=50,
            with_payload=True,
            with_vectors=False,
        )
        # Sort by timestamp ascending
        payloads = [r.payload for r in results if r.payload]
        payloads.sort(key=lambda p: p.get("timestamp", ""))
        return payloads
    except Exception as e:
        logger.warning("Failed to fetch task_changes for %s: %s", task_id, e)
        return []


def _fallback_memoir(task: Optional[dict], changes: list[dict]) -> str:
    """Deterministic memoir when GLM is unavailable."""
    title = (task or {}).get("content", "Unknown task")[:120] if task else "Unknown task"
    lines = [f"## Task\n\n{title}\n"]
    if changes:
        lines.append("## Changes\n")
        for ch in changes:
            lines.append(ch.get("content", ""))
    else:
        lines.append("_No changes recorded._")
    return "\n\n".join(lines)


async def _glm_memoir(task: Optional[dict], changes: list[dict], task_id: str) -> str:
    from app.services.llm_gateway import get_cloud_gateway

    task_content = (task or {}).get("content", "No description available.")
    changes_text = "\n\n".join(
        f"**Change {i+1}:**\n{ch.get('content', '')}"
        for i, ch in enumerate(changes)
    ) or "_No changes recorded._"

    prompt = f"""You are writing a brief technical retrospective for a completed task.

Original task:
{task_content}

Changes made during discussion/implementation:
{changes_text}

Write a concise memoir (3-5 paragraphs max) in Markdown covering:
1. What was originally planned
2. What changed and why (key decisions)
3. What was ultimately built

Be specific and factual. Focus on the *reasons* for decisions, not just what was done."""

    return await get_cloud_gateway().generate(
        prompt,
        system="You write concise technical retrospectives with minimal fluff.",
        task_type="text_summarization",
        mode="economy",
        max_tokens=420,
        temperature=0.2,
        allow_local_fallback=True,
        prefer_local=True,
    )


async def generate_memoir(
    task_id: str,
    qdrant_client: AsyncQdrantClient,
    collection: str,
) -> str:
    """
    Generate memoir content for a task. Does NOT store — just returns the text.
    Useful for preview or manual memoir generation.
    """
    task = await _fetch_task(task_id, qdrant_client, collection)
    changes = await _fetch_task_changes(task_id, qdrant_client, collection)

    from app.services.cloud_llm import cloud_available
    if cloud_available():
        try:
            return await _glm_memoir(task, changes, task_id)
        except Exception as e:
            logger.warning("GLM memoir failed for %s, using fallback: %s", task_id, e)

    return _fallback_memoir(task, changes)


async def generate_and_store_memoir(
    task_id: str,
    qdrant_client: AsyncQdrantClient,
    collection: str,
    ollama,
    agent_id: str = "claude",
    project: str = "mnemoforge",
) -> Optional[str]:
    """
    Generate memoir and store it with SQLite as canonical content storage and
    Qdrant as the semantic index.
    Returns the stored memory UUID, or None on failure.
    """
    content = await generate_memoir(task_id, qdrant_client, collection)

    task = await _fetch_task(task_id, qdrant_client, collection)
    changes = await _fetch_task_changes(task_id, qdrant_client, collection)
    readiness = memoir_generation_preconditions(task, changes)
    title = ""
    if task:
        first_line = task.get("content", "").splitlines()[0][:80]
        title = first_line

    full_content = f"# Memoir: {title}\n\n{content}" if title else content
    quality_status = memoir_quality_status(task, changes, full_content)
    if not readiness["ready"] and quality_status == "weak":
        logger.info(
            "Skipping memoir for task %s: weak preconditions (%s)",
            task_id,
            ", ".join(readiness["reasons"]) or "unknown",
        )
        return None

    try:
        memory_id = str(uuid4())
        now = datetime.now(timezone.utc)
        payload = {
            "content": _memoir_payload_ref(memory_id),
            "agent_id": agent_id,
            "memory_type": "experience",
            "category": "task_memoir",
            "importance_score": 0.7,
            "timestamp": now.isoformat(),
            "source": f"memoir:{task_id}",
            "tags": [f"task_id:{task_id}", "memoir", f"project:{project}"],
            "access_count": 0,
            "session_id": None,
            "decay_rate": 0.5,
            "project": project,
            "meta": {
                "entity_type": "decision_memoir",
                "task_id": task_id,
                "quality_status": quality_status,
                "change_count": len(changes),
                "task_present": bool(task),
                "readiness_status": "ready" if readiness["ready"] else "degraded",
                "readiness_reasons": list(readiness["reasons"]),
                "task_status": readiness["task_status"],
            },
        }
        await get_memory_store().upsert(
            memory_id=memory_id,
            category="task_memoir",
            content=full_content,
            metadata=_store_metadata_from_payload(payload),
        )
        vector, embedding_meta = await embed_text(
            full_content[:500],
            primary=ollama,
            purpose="task_memoir",
            fallback_reason="task_memoir_embedding_unavailable",
        )
        payload["meta"] = {**(payload.get("meta") or {}), **embedding_meta}
        await qdrant_client.upsert(
            collection_name=collection,
            points=[qmodels.PointStruct(
                id=memory_id,
                vector=vector,
                payload=payload,
            )],
        )
        logger.info("Memoir stored for task %s -> memory %s", task_id, memory_id)
        return memory_id
    except Exception as e:
        logger.error("Failed to store memoir for task %s: %s", task_id, e)
        return None


async def backfill_legacy_memoirs_to_store(
    qdrant_client: AsyncQdrantClient,
    collection: str,
    *,
    limit: int = 500,
    rewrite_qdrant_refs: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Backfill legacy task_memoir records whose full content still lives in Qdrant payloads.

    For legacy rows:
      - copy full content into SQLite-backed MemoryContentStore
      - optionally rewrite Qdrant payload.content to memoir_ref:<id>

    The operation is idempotent and safe to rerun.
    """
    store = get_memory_store()
    scanned = 0
    legacy_candidates = 0
    copied_to_sqlite = 0
    already_in_sqlite = 0
    rewritten_qdrant_refs = 0
    already_ref_payload = 0
    dangling_ref_payload = 0
    failed = 0
    failed_ids: list[str] = []
    offset = None

    while scanned < limit:
        batch_size = min(200, max(limit - scanned, 1))
        results, next_offset = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value="task_memoir"),
                )
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
                if _is_memoir_payload_ref(content):
                    if await store.exists(point_id):
                        already_ref_payload += 1
                    else:
                        dangling_ref_payload += 1
                    continue

                if not isinstance(content, str) or not content.strip():
                    continue

                legacy_candidates += 1
                exists_in_store = await store.exists(point_id)
                if exists_in_store:
                    already_in_sqlite += 1
                elif not dry_run:
                    await store.upsert(
                        memory_id=point_id,
                        category="task_memoir",
                        content=content,
                        metadata=_store_metadata_from_payload(payload),
                        created_at=_payload_created_at(payload),
                    )
                    copied_to_sqlite += 1

                if rewrite_qdrant_refs and not dry_run:
                    await qdrant_client.set_payload(
                        collection_name=collection,
                        payload={"content": _memoir_payload_ref(point_id)},
                        points=[point.id],
                    )
                    rewritten_qdrant_refs += 1
            except Exception as exc:
                failed += 1
                failed_ids.append(point_id)
                logger.warning("Legacy memoir backfill skipped %s: %s", point_id, exc)

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
        "dangling_ref_payload": dangling_ref_payload,
        "failed": failed,
        "failed_ids": failed_ids,
    }
