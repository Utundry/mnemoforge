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
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings

logger = logging.getLogger(__name__)


async def _fetch_task(
    task_id: str,
    qdrant_client: AsyncQdrantClient,
    collection: str,
) -> Optional[dict]:
    """Fetch the original task memory by UUID."""
    try:
        results = await qdrant_client.retrieve(
            collection_name=collection,
            ids=[task_id],
            with_payload=True,
            with_vectors=False,
        )
        return results[0].payload if results else None
    except Exception as e:
        logger.warning("Failed to fetch task %s: %s", task_id, e)
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
    from app.services.cloud_llm import cloud_complete

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

    return await cloud_complete(prompt, max_tokens=600, temperature=0.2)


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
    project: str = "supermemory",
) -> Optional[str]:
    """
    Generate memoir and store it as category=task_memoir in Qdrant.
    Returns the stored memory UUID, or None on failure.
    """
    content = await generate_memoir(task_id, qdrant_client, collection)

    task = await _fetch_task(task_id, qdrant_client, collection)
    title = ""
    if task:
        first_line = task.get("content", "").splitlines()[0][:80]
        title = first_line

    full_content = f"# Memoir: {title}\n\n{content}" if title else content

    try:
        vector = await ollama.embed(full_content[:500])
        memory_id = str(uuid4())
        now = datetime.now(timezone.utc)
        await qdrant_client.upsert(
            collection_name=collection,
            points=[qmodels.PointStruct(
                id=memory_id,
                vector=vector,
                payload={
                    "content": full_content,
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
                },
            )],
        )
        logger.info("Memoir stored for task %s -> memory %s", task_id, memory_id)
        return memory_id
    except Exception as e:
        logger.error("Failed to store memoir for task %s: %s", task_id, e)
        return None
