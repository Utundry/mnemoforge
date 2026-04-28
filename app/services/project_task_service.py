from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from threading import Lock
from typing import Optional
from uuid import UUID, uuid4

from qdrant_client.http import models as qmodels

from app.core.exceptions import MemoryNotFoundError, QdrantServiceError, VectorDimensionMismatchError
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate, MemoryUpdate
from app.models.project_task import (
    ProjectTaskBackfillResponse,
    ProjectTaskChangeCreate,
    ProjectTaskChangeRecord,
    ProjectTaskCreate,
    ProjectTaskReopenRequest,
    ProjectTaskRecord,
)
from app.services.improvements_store import get_improvements_store
from app.services.learning_store import get_learning_store
from app.services.project_tasks_content import build_task_content, build_task_change_content
from app.services.task_capture_rules import compute_task_statement_incomplete
from app.services.project_tasks_store import get_project_tasks_store
from app.services.text_localization import normalize_text_for_display

TASK_CATEGORY = "task"
TASK_CHANGE_CATEGORY = "task_change"
TASK_ENTITY_TAG = "entity:task"
TASK_CHANGE_ENTITY_TAG = "entity:task_change"

logger = logging.getLogger(__name__)

_TASK_TITLE_MAX = 256
_TASK_DESCRIPTION_MAX = 10000
_PROJECT_MAX = 128
_AGENT_ID_MAX = 256
_TOPIC_PATH_MAX = 256
_LINKED_IMPROVEMENT_MAX = 64
_TASK_MEMORY_CONTENT_MAX = 10000
_UTCNOW_LOCK = Lock()
_LAST_UTCNOW: datetime | None = None

def _task_store():
    return get_project_tasks_store()


def _utcnow() -> datetime:
    global _LAST_UTCNOW
    now = datetime.now(timezone.utc)
    with _UTCNOW_LOCK:
        if _LAST_UTCNOW is not None and now <= _LAST_UTCNOW:
            now = _LAST_UTCNOW + timedelta(microseconds=1)
        _LAST_UTCNOW = now
    return now


def _task_tag(task_id: str) -> str:
    return f"task_id:{task_id}"


def _normalize_task_id(task_id: str, *, project: str | None = None) -> str:
    clean = str(task_id or "").strip()
    if not clean:
        return ""
    parts = clean.split(":", 2)
    if len(parts) != 3:
        return clean
    kind, key_project, local_id = (part.strip() for part in parts)
    if kind != "task" or not local_id:
        return clean
    expected_project = str(project or "").strip()
    if expected_project and key_project and key_project != expected_project:
        return clean
    return local_id


def _qdrant_client(qdrant):
    return getattr(qdrant, "_client", qdrant)


def _qdrant_collection(qdrant):
    collection = getattr(qdrant, "_collection", None)
    if not collection:
        raise ValueError("Qdrant collection is required for project task operations")
    return collection


def _fit_task_texts_for_memory(title: str, description: str) -> tuple[str, str]:
    clean_title = normalize_text_for_display(str(title or ""))[:_TASK_TITLE_MAX].strip()
    if not clean_title:
        clean_title = "Untitled task"
    clean_description = normalize_text_for_display(str(description or ""))[:_TASK_DESCRIPTION_MAX]

    max_description = _TASK_MEMORY_CONTENT_MAX - len(clean_title)
    if clean_description:
        max_description -= 2  # "\n\n" separator added by build_task_content
    if max_description <= 0:
        return clean_title[:_TASK_MEMORY_CONTENT_MAX], ""
    if len(clean_description) > max_description:
        clean_description = clean_description[:max_description].rstrip()
    return clean_title, clean_description


async def _qdrant_get(qdrant, memory_id):
    getter = getattr(qdrant, "get", None)
    if callable(getter):
        return await getter(memory_id)
    results = await _qdrant_client(qdrant).retrieve(
        collection_name=_qdrant_collection(qdrant),
        ids=[str(memory_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise ValueError("Task memory not found")
    from app.services.qdrant_service import _point_to_record

    return _point_to_record(results[0])


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _task_record_from_memory(record, *, changes: Optional[list[ProjectTaskChangeRecord]] = None) -> ProjectTaskRecord:
    meta = dict(record.meta or {})
    created_raw = meta.get("created_at") or record.timestamp.isoformat()
    updated_raw = meta.get("updated_at") or created_raw
    return ProjectTaskRecord(
        id=record.id,
        task_id=str(meta.get("task_id") or record.id),
        project=record.project or "",
        title=normalize_text_for_display(str(meta.get("title") or record.content.splitlines()[0][:256])),
        description=normalize_text_for_display(str(meta.get("description") or "")),
        agent_id=record.agent_id,
        status=record.status or "planning",
        source=record.source,
        tags=list(record.tags or []),
        topic_path=record.topic_path,
        linked_improvement_id=meta.get("linked_improvement_id"),
        created_at=datetime.fromisoformat(created_raw),
        updated_at=datetime.fromisoformat(updated_raw),
        changes=changes or [],
    )


def _store_row_to_task(row: dict, *, changes: Optional[list[ProjectTaskChangeRecord]] = None) -> ProjectTaskRecord:
    created_at = datetime.fromtimestamp(row["created_at"], tz=timezone.utc)
    updated_at = datetime.fromtimestamp(row["updated_at"], tz=timezone.utc)
    return ProjectTaskRecord(
        id=UUID(str(row["id"])),
        task_id=str(row["task_id"]),
        project=str(row["project"]),
        title=str(row["title"]),
        description=str(row["description"]),
        agent_id=str(row["agent_id"]),
        status=str(row["status"]),
        source=str(row["source"]),
        tags=list(row.get("tags") or []),
        topic_path=row.get("topic_path"),
        linked_improvement_id=row.get("linked_improvement_id"),
        created_at=created_at,
        updated_at=updated_at,
        changes=changes or [],
    )


def _artifact_tags(row: dict) -> set[str]:
    return {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}


_TASK_CAPTURE_ARTIFACT_TYPES = (
    "task_capture_candidate",
    "decision_candidate",
    "chosen_decision",
    "code_link",
    "remaining_risk",
)


async def _task_capture_summary_map(project: str, *, limit_hint: int = 200) -> dict[str, dict[str, int | bool]]:
    store = get_learning_store()
    per_task: dict[str, dict[str, int | bool]] = {}
    project_tag = f"project:{project}"
    fetch_limit = max(limit_hint * 6, 200)

    for status in ("active", "archived"):
        for artifact_type in _TASK_CAPTURE_ARTIFACT_TYPES:
            rows = await store.list_artifacts(
                artifact_type=artifact_type,
                scope="project",
                status=status,
                limit=fetch_limit,
            )
            for row in rows:
                tags = _artifact_tags(row)
                if project_tag not in tags:
                    continue
                task_tag = next((tag for tag in tags if tag.startswith("task_id:")), "")
                if not task_tag:
                    continue
                task_id = task_tag.split(":", 1)[1].strip()
                if not task_id:
                    continue
                summary = per_task.setdefault(
                    task_id,
                    {
                        "task_capture_pending_count": 0,
                        "task_capture_promoted_count": 0,
                        "task_statement_incomplete": False,
                    },
                )
                if status == "active":
                    summary["task_capture_pending_count"] = int(summary["task_capture_pending_count"]) + 1
                    summary["task_statement_incomplete"] = True
                elif status == "archived":
                    summary["task_capture_promoted_count"] = int(summary["task_capture_promoted_count"]) + 1
    return per_task


def _apply_task_capture_summary(
    task: ProjectTaskRecord,
    summary_map: dict[str, dict[str, int | bool]],
) -> ProjectTaskRecord:
    summary = summary_map.get(task.task_id) or {}
    pending_count = int(summary.get("task_capture_pending_count") or 0)
    task.task_capture_pending_count = pending_count
    task.task_capture_promoted_count = int(summary.get("task_capture_promoted_count") or 0)
    task.task_statement_incomplete = compute_task_statement_incomplete(
        title=task.title,
        description=task.description,
        status=task.status,
        changes=task.changes,
        pending_capture_count=pending_count,
    )
    return task


def _store_row_to_change(row: dict) -> ProjectTaskChangeRecord:
    timestamp = datetime.fromtimestamp(row["created_at"], tz=timezone.utc)
    return ProjectTaskChangeRecord(
        id=UUID(str(row["id"])),
        task_id=str(row["task_id"]),
        project=str(row["project"]),
        change_type=str(row["change_type"]),
        content=str(row["content"]),
        why=str(row["why"]),
        agent_id=str(row["agent_id"]),
        source=str(row["source"]),
        tags=list(row.get("tags") or []),
        timestamp=timestamp,
    )


async def _persist_task_memory(record) -> None:
    if not record:
        return
    task_record = _task_record_from_memory(record)
    _task_store().upsert_task(
        memory_id=str(task_record.id),
        task_id=task_record.task_id,
        project=task_record.project,
        title=task_record.title,
        description=task_record.description,
        agent_id=task_record.agent_id,
        status=task_record.status,
        source=task_record.source,
        tags=task_record.tags,
        topic_path=task_record.topic_path,
        linked_improvement_id=task_record.linked_improvement_id,
        created_at=task_record.created_at.timestamp(),
        updated_at=task_record.updated_at.timestamp(),
    )


def _persist_task_change(record) -> None:
    if not record:
        return
    meta = dict(record.meta or {})
    task_id = str(meta.get("task_id") or "")
    project = record.project or ""
    change_type = str(meta.get("change_type") or "note")
    why = str(meta.get("why") or "")
    _task_store().add_change(
        memory_id=str(record.id),
        task_id=task_id,
        project=project,
        change_type=change_type,
        content=str(record.content),
        why=why,
        agent_id=record.agent_id,
        source=record.source,
        tags=list(record.tags or []),
        created_at=record.timestamp.timestamp(),
    )


def _task_change_record_from_memory(record) -> ProjectTaskChangeRecord:
    meta = dict(record.meta or {})
    return ProjectTaskChangeRecord(
        id=record.id,
        task_id=str(meta.get("task_id") or ""),
        project=record.project or "",
        change_type=str(meta.get("change_type") or "note"),
        content=normalize_text_for_display(record.content),
        why=normalize_text_for_display(str(meta.get("why") or "")),
        agent_id=record.agent_id,
        source=record.source,
        tags=list(record.tags or []),
        timestamp=record.timestamp,
    )


async def _find_task_memory_id_qdrant(qdrant, *, project: str, task_id: str) -> Optional[str]:
    rows, _ = await _qdrant_client(qdrant).scroll(
        collection_name=_qdrant_collection(qdrant),
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=TASK_CATEGORY)),
            qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
            qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=_task_tag(task_id))),
        ]),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    if not rows:
        return None
    return str(rows[0].id)


async def _find_task_memory_id(qdrant, *, project: str, task_id: str) -> Optional[str]:
    task_id = _normalize_task_id(task_id, project=project)
    stored = _task_store().get_task_by_task_id(project=project, task_id=task_id)
    if stored:
        return str(stored["id"])
    memory_id = await _find_task_memory_id_qdrant(qdrant, project=project, task_id=task_id)
    if memory_id:
        record = await _qdrant_get(qdrant, memory_id)
        await _persist_task_memory(record)
    return memory_id


async def _resolve_task_project(qdrant, *, task_id: str, project: str | None = None) -> str:
    clean_task_id = _normalize_task_id(task_id, project=project)
    if not clean_task_id:
        raise ValueError("Task not found")

    clean_project = str(project or "").strip()
    if clean_project:
        stored = _task_store().get_task_by_task_id(project=clean_project, task_id=clean_task_id)
        if stored:
            return clean_project
        memory_id = await _find_task_memory_id(qdrant, project=clean_project, task_id=clean_task_id)
        if memory_id:
            return clean_project
        raise ValueError("Task not found")

    stored_rows = _task_store().list_tasks(task_id=clean_task_id, limit=2)
    if len(stored_rows) == 1:
        return str(stored_rows[0]["project"])
    if len(stored_rows) > 1:
        projects = sorted({str(row.get("project") or "").strip() for row in stored_rows if str(row.get("project") or "").strip()})
        if len(projects) == 1:
            return projects[0]
        raise ValueError(f"Task {clean_task_id} is ambiguous across projects; provide project")

    rows, _ = await _qdrant_client(qdrant).scroll(
        collection_name=_qdrant_collection(qdrant),
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=TASK_CATEGORY)),
            qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=_task_tag(clean_task_id))),
        ]),
        limit=2,
        with_payload=True,
        with_vectors=False,
    )
    projects: list[str] = []
    for row in rows:
        record = await _qdrant_get(qdrant, row.id)
        await _persist_task_memory(record)
        if record.project:
            projects.append(record.project)
    unique_projects = sorted(set(projects))
    if len(unique_projects) == 1:
        return unique_projects[0]
    if len(unique_projects) > 1:
        raise ValueError(f"Task {clean_task_id} is ambiguous across projects; provide project")
    raise ValueError("Task not found")


async def create_or_update_project_task(qdrant, ollama, body: ProjectTaskCreate) -> ProjectTaskRecord:
    task_id = (body.task_id or str(uuid4())).strip()
    now_dt = _utcnow()
    now = now_dt.isoformat()
    clean_title, clean_description = _fit_task_texts_for_memory(body.title, body.description)
    existing_id = await _find_task_memory_id(qdrant, project=body.project, task_id=task_id)

    if existing_id:
        try:
            current = await _qdrant_get(qdrant, existing_id)
        except MemoryNotFoundError:
            _task_store().delete_task(memory_id=existing_id)
            existing_id = None

    if existing_id:
        meta = dict(current.meta or {})
        created_at = meta.get("created_at") or current.timestamp.isoformat()
        meta.update({
            "entity_type": "project_task",
            "task_id": task_id,
            "title": clean_title,
            "description": clean_description,
            "updated_at": now,
            "created_at": created_at,
            "linked_improvement_id": body.linked_improvement_id or meta.get("linked_improvement_id"),
        })
        tags = _unique(
            [tag for tag in (current.tags or []) if not str(tag).startswith("task_status:")]
            + list(body.tags or [])
            + [TASK_ENTITY_TAG, _task_tag(task_id), f"task_status:{body.status}", f"project:{body.project}"]
        )
        updated = await qdrant.update(
            current.id,
            MemoryUpdate(
                content=build_task_content(clean_title, clean_description),
                category=TASK_CATEGORY,
                memory_type=MemoryType.task,
                source=body.source,
                tags=tags,
                status=body.status,
                meta=meta,
                project=body.project,
                topic_path=body.topic_path,
                scope="project",
            ),
            new_vector=await ollama.embed(build_task_content(clean_title, clean_description)),
        )
        await _persist_task_memory(updated)
        return _task_record_from_memory(updated)

    memory = MemoryCreate(
        content=build_task_content(clean_title, clean_description),
        agent_id=body.agent_id,
        memory_type=MemoryType.task,
        category=TASK_CATEGORY,
        importance_score=0.8,
        source=body.source,
        tags=_unique(list(body.tags or []) + [TASK_ENTITY_TAG, _task_tag(task_id), f"task_status:{body.status}", f"project:{body.project}"]),
        project=body.project,
        topic_path=body.topic_path,
        scope="project",
        status=body.status,
        meta={
            "entity_type": "project_task",
            "task_id": task_id,
            "title": clean_title,
            "description": clean_description,
            "created_at": now,
            "updated_at": now,
            "linked_improvement_id": body.linked_improvement_id,
        },
    )
    task_memory_id = await qdrant.insert(memory, await ollama.embed(memory.content))
    created = await _qdrant_get(qdrant, task_memory_id)
    await _persist_task_memory(created)
    return _task_record_from_memory(created)


async def get_project_task(qdrant, *, project: str, task_id: str, include_changes: bool = False) -> ProjectTaskRecord:
    task_id = _normalize_task_id(task_id, project=project)
    stored = _task_store().get_task_by_task_id(project=project, task_id=task_id)
    if stored:
        changes = await list_task_changes(qdrant, project=project, task_id=task_id)
        task = _store_row_to_task(stored, changes=changes)
        task = _apply_task_capture_summary(task, await _task_capture_summary_map(project, limit_hint=50))
        if not include_changes:
            task.changes = []
        return task
    memory_id = await _find_task_memory_id(qdrant, project=project, task_id=task_id)
    if not memory_id:
        raise ValueError("Task not found")
    record = await _qdrant_get(qdrant, memory_id)
    changes = await list_task_changes(qdrant, project=project, task_id=task_id)
    await _persist_task_memory(record)
    task = _task_record_from_memory(record, changes=changes)
    task = _apply_task_capture_summary(task, await _task_capture_summary_map(project, limit_hint=50))
    if not include_changes:
        task.changes = []
    return task


def _matches_time_filters(
    task: ProjectTaskRecord,
    *,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    updated_after: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
) -> bool:
    if created_after and task.created_at < created_after:
        return False
    if created_before and task.created_at > created_before:
        return False
    if updated_after and task.updated_at < updated_after:
        return False
    if updated_before and task.updated_at > updated_before:
        return False
    return True


async def list_project_tasks(
    qdrant,
    *,
    project: str,
    status: Optional[str] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    updated_after: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
    limit: int = 50,
) -> list[ProjectTaskRecord]:
    stored_rows = _task_store().list_tasks(
        project=project,
        status=status,
        created_after=created_after.timestamp() if created_after else None,
        created_before=created_before.timestamp() if created_before else None,
        updated_after=updated_after.timestamp() if updated_after else None,
        updated_before=updated_before.timestamp() if updated_before else None,
        limit=limit,
    )
    if stored_rows:
        summary_map = await _task_capture_summary_map(project, limit_hint=limit)
        items: list[ProjectTaskRecord] = []
        for row in stored_rows:
            changes = await list_task_changes(qdrant, project=project, task_id=str(row["task_id"]), limit=20)
            task = _store_row_to_task(row, changes=changes)
            if not _matches_time_filters(
                task,
                created_after=created_after,
                created_before=created_before,
                updated_after=updated_after,
                updated_before=updated_before,
            ):
                continue
            task = _apply_task_capture_summary(task, summary_map)
            task.changes = []
            items.append(task)
        return items
    return await _list_project_tasks_qdrant(
        qdrant,
        project=project,
        status=status,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        limit=limit,
    )


async def _list_project_tasks_qdrant(
    qdrant,
    *,
    project: str,
    status: Optional[str],
    created_after: Optional[datetime],
    created_before: Optional[datetime],
    updated_after: Optional[datetime],
    updated_before: Optional[datetime],
    limit: int,
) -> list[ProjectTaskRecord]:
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=TASK_CATEGORY)),
        qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
    ]
    if status and status != "all":
        must.append(qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value=status)))
    rows, _ = await _qdrant_client(qdrant).scroll(
        collection_name=_qdrant_collection(qdrant),
        scroll_filter=qmodels.Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    items: list[ProjectTaskRecord] = []
    for row in rows:
        payload = row.payload or {}
        if payload.get("meta", {}).get("entity_type") != "project_task":
            continue
        record = await _qdrant_get(qdrant, row.id)
        await _persist_task_memory(record)
        changes = await list_task_changes(qdrant, project=project, task_id=str((record.meta or {}).get("task_id") or row.id), limit=20)
        task = _task_record_from_memory(record, changes=changes)
        if not _matches_time_filters(
            task,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
        ):
            continue
        items.append(task)
    items.sort(key=lambda item: item.updated_at, reverse=True)
    summary_map = await _task_capture_summary_map(project, limit_hint=limit)
    result: list[ProjectTaskRecord] = []
    for item in items[:limit]:
        item = _apply_task_capture_summary(item, summary_map)
        item.changes = []
        result.append(item)
    return result


async def add_task_change(qdrant, ollama, *, task_id: str, body: ProjectTaskChangeCreate) -> ProjectTaskChangeRecord:
    project = body.project
    task_id = _normalize_task_id(task_id, project=project)
    if not project:
        task = None
        rows, _ = await _qdrant_client(qdrant).scroll(
            collection_name=_qdrant_collection(qdrant),
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=TASK_CATEGORY)),
                qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=_task_tag(task_id))),
            ]),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if rows:
            task = await _qdrant_get(qdrant, rows[0].id)
        if not task:
            raise ValueError("Task not found")
        project = task.project or ""
        task_id = _normalize_task_id(task_id, project=project)
    elif not await _find_task_memory_id(qdrant, project=project, task_id=task_id):
        raise ValueError("Task not found")

    memory = MemoryCreate(
        content=build_task_change_content(body.change_type, body.content, body.why),
        agent_id=body.agent_id,
        memory_type=MemoryType.experience,
        category=TASK_CHANGE_CATEGORY,
        importance_score=0.72,
        source=body.source,
        tags=_unique(list(body.tags or []) + [TASK_CHANGE_ENTITY_TAG, _task_tag(task_id), f"project:{project}"]),
        project=project,
        scope="project",
        meta={
            "entity_type": "task_change",
            "task_id": task_id,
            "change_type": body.change_type,
            "why": body.why.strip(),
            "created_at": _utcnow().isoformat(),
        },
    )
    memory_id = await qdrant.insert(memory, await ollama.embed(memory.content))
    record = await _qdrant_get(qdrant, memory_id)
    _persist_task_change(record)
    return _task_change_record_from_memory(record)


async def reopen_project_task(
    qdrant,
    ollama,
    *,
    task_id: str,
    body: ProjectTaskReopenRequest,
) -> ProjectTaskRecord:
    project = await _resolve_task_project(qdrant, task_id=task_id, project=body.project)
    task = await get_project_task(qdrant, project=project, task_id=task_id, include_changes=False)
    target_status = body.status
    if task.status == target_status:
        reopened_reason = f"Task already in {target_status}; recorded reopen request."
    else:
        reopened_reason = f"Reopened task to {target_status}."

    await add_task_change(
        qdrant,
        ollama,
        task_id=task_id,
        body=ProjectTaskChangeCreate(
            project=project,
            change_type="status_change",
            content=reopened_reason,
            why=body.reason,
            agent_id=body.acted_by,
            source=body.source,
            tags=list(task.tags or []),
        ),
    )

    reopened = await create_or_update_project_task(
        qdrant,
        ollama,
        ProjectTaskCreate(
            task_id=task.task_id,
            project=project,
            title=task.title,
            description=task.description,
            agent_id=task.agent_id,
            status=target_status,
            source=body.source,
            tags=list(task.tags or []),
            topic_path=task.topic_path,
            linked_improvement_id=task.linked_improvement_id,
        ),
    )
    return reopened


async def list_task_changes(qdrant, *, project: str, task_id: str, limit: int = 100) -> list[ProjectTaskChangeRecord]:
    stored_rows = _task_store().list_changes(project=project, task_id=task_id, limit=limit)
    if stored_rows:
        return [_store_row_to_change(row) for row in stored_rows]
    return await _list_task_changes_qdrant(qdrant, project=project, task_id=task_id, limit=limit)


async def _list_task_changes_qdrant(qdrant, *, project: str, task_id: str, limit: int) -> list[ProjectTaskChangeRecord]:
    rows, _ = await _qdrant_client(qdrant).scroll(
        collection_name=_qdrant_collection(qdrant),
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=TASK_CHANGE_CATEGORY)),
            qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
            qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=_task_tag(task_id))),
        ]),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    items: list[ProjectTaskChangeRecord] = []
    for row in rows:
        payload = row.payload or {}
        if payload.get("meta", {}).get("entity_type") != "task_change":
            continue
        record = await _qdrant_get(qdrant, row.id)
        _persist_task_change(record)
        items.append(_task_change_record_from_memory(record))
    items.sort(key=lambda item: item.timestamp)
    return items


async def ensure_task_for_improvement(qdrant, ollama, row: dict) -> ProjectTaskRecord:
    task_id = str(row.get("id") or "").strip()
    if not task_id:
        raise ValueError("improvement row is missing id")

    project = str(row.get("project") or "").strip()[:_PROJECT_MAX]
    if not project:
        raise ValueError(f"improvement {task_id} is missing project")

    title = normalize_text_for_display(str(row.get("title") or ""))[:_TASK_TITLE_MAX].strip()
    if not title:
        title = f"Improvement {task_id}"[:_TASK_TITLE_MAX]

    description = normalize_text_for_display(str(row.get("description") or ""))[:_TASK_DESCRIPTION_MAX]
    agent_id = normalize_text_for_display(str(row.get("agent_id") or "system"))[:_AGENT_ID_MAX] or "system"
    topic_path_raw = row.get("topic_path")
    topic_path = normalize_text_for_display(str(topic_path_raw))[:_TOPIC_PATH_MAX] if topic_path_raw else None

    raw_tags = row.get("tags") or []
    if isinstance(raw_tags, str):
        tags = [raw_tags]
    elif isinstance(raw_tags, (list, tuple, set)):
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    else:
        tags = []

    return await create_or_update_project_task(
        qdrant,
        ollama,
        ProjectTaskCreate(
            task_id=task_id,
            project=project,
            title=title,
            description=description,
            agent_id=agent_id,
            status="done" if row.get("status") == "resolved" else "planning",
            source="improvement",
            tags=tags,
            linked_improvement_id=task_id[:_LINKED_IMPROVEMENT_MAX],
            topic_path=topic_path,
        ),
    )


async def record_improvement_task_change(
    qdrant,
    ollama,
    *,
    improvement_row: dict,
    change_type: str,
    content: str,
    why: str = "",
    source: str = "improvement",
) -> ProjectTaskChangeRecord:
    return await add_task_change(
        qdrant,
        ollama,
        task_id=str(improvement_row["id"]),
        body=ProjectTaskChangeCreate(
            project=improvement_row["project"],
            change_type=change_type,
            content=content,
            why=why,
            agent_id=improvement_row.get("agent_id") or "system",
            source=source,
            tags=list(improvement_row.get("tags") or []),
        ),
    )


async def backfill_tasks_from_improvements(
    qdrant,
    ollama,
    *,
    project: Optional[str] = None,
    limit: int = 500,
) -> ProjectTaskBackfillResponse:
    rows = await get_improvements_store().list(project=project, status=None, limit=limit)
    created = 0
    skipped_existing = 0
    failed = 0
    failed_task_ids: list[str] = []
    for row in rows:
        row_id = str(row.get("id") or "").strip() or "<missing-id>"
        row_project = str(row.get("project") or "").strip() or "<missing-project>"
        try:
            if row_id == "<missing-id>":
                raise ValueError("improvement row is missing id")
            if row_project == "<missing-project>":
                raise ValueError(f"improvement {row_id} is missing project")

            existing_id = await _find_task_memory_id(qdrant, project=row_project, task_id=row_id)
            if existing_id:
                skipped_existing += 1
                continue
            await ensure_task_for_improvement(qdrant, ollama, row)
            created += 1
        except Exception as exc:
            failed += 1
            failed_task_ids.append(row_id)
            logger.warning(
                "Task backfill skipped improvement %s for project %s: %s",
                row_id,
                row_project,
                exc,
            )
    return ProjectTaskBackfillResponse(
        project=project,
        scanned=len(rows),
        created=created,
        skipped_existing=skipped_existing,
        failed=failed,
        failed_task_ids=failed_task_ids,
    )
