from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings
from app.services.ollama_service import OllamaService
from app.services.project_tasks_content import build_task_content, build_task_change_content
from app.services.project_tasks_store import get_project_tasks_store


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def _upsert_task_memory(
    client: AsyncQdrantClient,
    ollama: OllamaService,
    row: dict,
) -> None:
    content = build_task_content(row["title"], row["description"])
    vector = await ollama.embed(content)
    payload = {
        "content": content,
        "agent_id": row.get("agent_id") or "system",
        "memory_type": "task",
        "category": "task",
        "importance_score": 0.8,
        "timestamp": _iso_from_ts(row["updated_at"]),
        "source": row.get("source") or "project-task",
        "tags": row.get("tags") or [],
        "access_count": 0,
        "session_id": None,
        "decay_rate": 1.0,
        "meta": {
            "entity_type": "project_task",
            "task_id": row["task_id"],
            "title": row["title"],
            "description": row["description"],
            "created_at": _iso_from_ts(row["created_at"]),
            "updated_at": _iso_from_ts(row["updated_at"]),
            "linked_improvement_id": row.get("linked_improvement_id"),
        },
        "project": row["project"],
        "status": row["status"],
        "topic_path": row.get("topic_path"),
        "scope": "project",
    }
    await client.upsert(
        collection_name=settings.qdrant_collection_name,
        points=[qmodels.PointStruct(id=row["id"], vector=vector, payload=payload)],
    )


async def _upsert_change_memory(
    client: AsyncQdrantClient,
    ollama: OllamaService,
    row: dict,
) -> None:
    content = build_task_change_content(row["change_type"], row["content"], row["why"])
    vector = await ollama.embed(content)
    payload = {
        "content": content,
        "agent_id": row.get("agent_id") or "system",
        "memory_type": "experience",
        "category": "task_change",
        "importance_score": 0.72,
        "timestamp": _iso_from_ts(row["created_at"]),
        "source": row.get("source") or "project-task",
        "tags": row.get("tags") or [],
        "access_count": 0,
        "session_id": None,
        "decay_rate": 1.0,
        "meta": {
            "entity_type": "task_change",
            "task_id": row["task_id"],
            "change_type": row["change_type"],
            "why": row["why"],
            "created_at": _iso_from_ts(row["created_at"]),
        },
        "project": row["project"],
        "scope": "project",
    }
    await client.upsert(
        collection_name=settings.qdrant_collection_name,
        points=[qmodels.PointStruct(id=row["id"], vector=vector, payload=payload)],
    )


async def rebuild_project_tasks(
    qdrant_client: AsyncQdrantClient,
    ollama: OllamaService,
    *,
    project: Optional[str] = None,
    limit: int = 0,
    changes_limit: int = 0,
) -> dict:
    """Rehydrate task/task_change memories from the durable SQLite store."""
    store = get_project_tasks_store()
    task_limit = limit if limit > 0 else 2000
    max_changes = changes_limit if changes_limit > 0 else 100
    tasks = store.list_tasks(project=project, status=None, limit=task_limit)
    if not tasks:
        return {"project": project or "all", "tasks": 0, "changes": 0}

    total_changes = 0
    for row in tasks:
        await _upsert_task_memory(qdrant_client, ollama, row)
        changes = store.list_changes(
            project=row["project"], task_id=row["task_id"], limit=max_changes
        )
        for change in changes:
            await _upsert_change_memory(qdrant_client, ollama, change)
        total_changes += len(changes)

    return {
        "project": project or "all",
        "tasks": len(tasks),
        "changes": total_changes,
    }
