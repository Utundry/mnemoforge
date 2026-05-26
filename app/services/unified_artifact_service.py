from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from app.models.unified_artifact import (
    ArtifactKey,
    UnifiedArtifactListResponse,
    UnifiedArtifactRecord,
    UnifiedArtifactReopenRequest,
    UnifiedArtifactResolveRequest,
    from_unified_status,
    SYNC_STATUS_MAPPING,
    to_unified_status,
)
from app.services.improvements_store import get_improvements_store
from app.services.project_identity_service import project_lookup_ids, resolve_project_id
from app.services.project_task_service import _task_capture_summary_map
from app.services.project_tasks_store import get_project_tasks_store

logger = logging.getLogger(__name__)


def _normalize_datetime_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _matches_datetime_range(
    value: datetime,
    *,
    after: datetime | None = None,
    before: datetime | None = None,
) -> bool:
    value = _normalize_datetime_utc(value) or value
    after = _normalize_datetime_utc(after)
    before = _normalize_datetime_utc(before)
    if after and value < after:
        return False
    if before and value > before:
        return False
    return True


def _matches_artifact_query(item: UnifiedArtifactRecord, query: str) -> bool:
    tokens = [
        token
        for token in str(query or "").casefold().split()
        if token and token not in {"about", "with", "task", "tasks", "improvement", "improvements", "artifact", "artifacts"}
    ]
    if not tokens:
        return True
    haystack = " ".join(
        str(value or "")
        for value in (
            item.artifact_key,
            item.linked_artifact_key,
            item.task_id,
            item.title,
            item.description,
            item.topic_path,
            " ".join(item.tags or []),
        )
    ).casefold()
    return all(token in haystack for token in tokens)


class UnifiedArtifactService:
    """Единый фасад для доступа к improvements и tasks."""

    def __init__(self):
        self._improvements_store = get_improvements_store()
        self._tasks_store = get_project_tasks_store()

    async def get_artifact(self, artifact_key: str) -> UnifiedArtifactRecord:
        """Получить сущность по artifact_key независимо от типа."""
        key = ArtifactKey.parse(artifact_key)
        key = ArtifactKey(type=key.type, project=resolve_project_id(key.project), local_id=key.local_id)

        if key.type == "improvement":
            return await self._get_improvement(key)
        elif key.type == "task":
            return await self._get_task(key)
        else:
            raise ValueError(f"Unsupported artifact type: {key.type}")

    async def _get_improvement(self, key: ArtifactKey) -> UnifiedArtifactRecord:
        """Получить improvement и преобразовать в UnifiedArtifactRecord."""
        improvement_id = key.to_uuid()
        row = await self._improvements_store.get(improvement_id)

        if not row:
            raise ValueError(f"Improvement not found: {improvement_id}")

        # Получить связанный task по task_id (= improvement_id) если есть
        canonical_project = resolve_project_id(row["project"])
        linked_artifact_key = None
        linked_status = None
        try:
            linked_task = None
            for lookup_project in project_lookup_ids(canonical_project):
                linked_task = self._tasks_store.get_task_by_task_id(
                    project=lookup_project,
                    task_id=str(improvement_id),
                )
                if linked_task:
                    break
            if linked_task:
                linked_artifact_key = f"task:{canonical_project}:{linked_task['task_id']}"
                linked_status = to_unified_status("task", linked_task["status"])
        except Exception as e:
            logger.warning(f"Failed to get linked task for improvement {improvement_id}: {e}")

        return UnifiedArtifactRecord(
            artifact_key=str(ArtifactKey(type="improvement", project=canonical_project, local_id=key.local_id)),
            type="improvement",
            id=improvement_id,
            project=canonical_project,
            title=row["title"],
            description=row["description"],
            status=to_unified_status("improvement", row["status"]),
            agent_id=row["agent_id"],
            tags=row.get("tags") or [],
            created_at=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
            updated_at=datetime.fromtimestamp(row.get("updated_at", row["created_at"]), tz=timezone.utc),
            # Improvement-specific fields
            importance_score=row.get("importance_score"),
            stage=row.get("stage") or "proposal",
            verdict=row.get("verdict") or None,
            resolved_at=datetime.fromtimestamp(row["resolved_at"], tz=timezone.utc) if row.get("resolved_at") else None,
            report_count=row.get("report_count"),
            # Linked artifact
            linked_artifact_key=linked_artifact_key,
            linked_status=linked_status,
        )

    async def _get_task(self, key: ArtifactKey) -> UnifiedArtifactRecord:
        """Получить task и преобразовать в UnifiedArtifactRecord."""
        row = None
        canonical_project = resolve_project_id(key.project)
        for lookup_project in project_lookup_ids(canonical_project):
            row = self._tasks_store.get_task_by_task_id(
                project=lookup_project,
                task_id=key.local_id,
            )
            if row:
                break

        if not row:
            raise ValueError(f"Task not found: {key.local_id}")

        # Получить связанный improvement, если есть
        summary = (await _task_capture_summary_map(canonical_project, limit_hint=1)).get(key.local_id) or {}

        linked_artifact_key = None
        linked_status = None
        if row.get("linked_improvement_id"):
            try:
                linked_improvement_id = UUID(row["linked_improvement_id"])
                linked_improvement = await self._improvements_store.get(linked_improvement_id)
                if linked_improvement:
                    linked_artifact_key = f"improvement:{canonical_project}:{linked_improvement_id}"
                    linked_status = to_unified_status("improvement", linked_improvement["status"])
            except (ValueError, Exception) as e:
                logger.warning(f"Failed to get linked improvement for task {key.local_id}: {e}")

        return UnifiedArtifactRecord(
            artifact_key=str(ArtifactKey(type="task", project=canonical_project, local_id=key.local_id)),
            type="task",
            id=UUID(row["id"]),
            project=canonical_project,
            title=row["title"],
            description=row["description"],
            status=to_unified_status("task", row["status"]),
            agent_id=row["agent_id"],
            tags=row.get("tags") or [],
            created_at=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
            updated_at=datetime.fromtimestamp(row["updated_at"], tz=timezone.utc),
            # Task-specific fields
            task_id=row["task_id"],
            source=row["source"],
            topic_path=row.get("topic_path"),
            task_capture_pending_count=summary.get("task_capture_pending_count", row.get("task_capture_pending_count", 0)),
            task_capture_promoted_count=summary.get("task_capture_promoted_count", row.get("task_capture_promoted_count", 0)),
            task_statement_incomplete=summary.get("task_statement_incomplete", row.get("task_statement_incomplete", False)),
            # Linked artifact
            linked_artifact_key=linked_artifact_key,
            linked_status=linked_status,
        )

    async def list_artifacts(
        self,
        project: str,
        status: str | None = None,
        type_: str | None = None,
        query: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        limit: int = 50,
    ) -> UnifiedArtifactListResponse:
        """Получить список сущностей с фильтрацией по типу и статусу."""
        canonical_project = resolve_project_id(project)
        lookup_projects = project_lookup_ids(canonical_project)
        items: list[UnifiedArtifactRecord] = []

        # Преобразовать unified status в тип-специфичный
        improvement_status = None
        task_status = None
        if status:
            # Для improvements
            improvement_status = from_unified_status("improvement", status)
            # Для tasks
            task_status = from_unified_status("task", status)

        # Получить improvements
        if type_ is None or type_ == "improvement":
            improvements = []
            for lookup_project in lookup_projects:
                improvements.extend(
                    await self._improvements_store.list(
                        project=lookup_project,
                        status=improvement_status,
                        limit=limit,
                    )
                )
            for imp in improvements:
                try:
                    key = ArtifactKey(type="improvement", project=canonical_project, local_id=str(imp["id"]))
                    items.append(await self._get_improvement(key))
                except Exception as e:
                    logger.warning(f"Failed to convert improvement {imp['id']}: {e}")

        # Получить tasks
        if type_ is None or type_ == "task":
            tasks = []
            for lookup_project in lookup_projects:
                tasks.extend(
                    self._tasks_store.list_tasks(
                        project=lookup_project,
                        status=task_status,
                        limit=limit,
                    )
                )
            for task in tasks:
                try:
                    key = ArtifactKey(type="task", project=canonical_project, local_id=task["task_id"])
                    items.append(await self._get_task(key))
                except Exception as e:
                    logger.warning(f"Failed to convert task {task['task_id']}: {e}")

        # Защита от рассинхрона стора и API-параметров: фильтруем по итоговому unified status.
        if status:
            items = [item for item in items if item.status == status]
        if created_after or created_before:
            items = [
                item for item in items
                if _matches_datetime_range(
                    item.created_at,
                    after=created_after,
                    before=created_before,
                )
            ]
        if updated_after or updated_before:
            items = [
                item for item in items
                if _matches_datetime_range(
                    item.updated_at,
                    after=updated_after,
                    before=updated_before,
                )
            ]
        if query:
            items = [item for item in items if _matches_artifact_query(item, query)]

        # Сортировать по updated_at (новые первыми)
        items.sort(key=lambda x: x.updated_at, reverse=True)

        # Ограничить количество
        items = items[:limit]

        return UnifiedArtifactListResponse(
            total=len(items),
            items=items,
        )

    async def resolve_artifact(
        self,
        artifact_key: str,
        request: UnifiedArtifactResolveRequest,
    ) -> UnifiedArtifactRecord:
        """Разрешить сущность (improvement→resolved, task→done)."""
        key = ArtifactKey.parse(artifact_key)

        if key.type == "improvement":
            return await self._resolve_improvement(key, request)
        elif key.type == "task":
            return await self._resolve_task(key, request)
        else:
            raise ValueError(f"Unsupported artifact type: {key.type}")

    async def _resolve_improvement(
        self,
        key: ArtifactKey,
        request: UnifiedArtifactResolveRequest,
    ) -> UnifiedArtifactRecord:
        """Разрешить improvement и синхронизировать с task."""
        improvement_id = key.to_uuid()

        # Разрешить improvement
        project = await self._improvements_store.resolve(
            improvement_id,
            acted_by=request.acted_by,
            action_source=request.action_source,
            reason=request.reason,
        )

        # Получить обновленный improvement
        row = await self._improvements_store.get(improvement_id)
        if not row:
            raise ValueError(f"Improvement not found after resolve: {improvement_id}")

        # Синхронизировать с task, если есть связанная task запись
        try:
            linked_task = self._tasks_store.get_task_by_task_id(
                project=row["project"],
                task_id=str(improvement_id),
            )
            if linked_task:
                self._tasks_store.upsert_task(
                    memory_id=str(linked_task["id"]),
                    task_id=linked_task["task_id"],
                    project=linked_task["project"],
                    title=linked_task["title"],
                    description=linked_task["description"],
                    agent_id=linked_task["agent_id"],
                    status="done",
                    source=linked_task["source"],
                    tags=linked_task.get("tags") or [],
                    topic_path=linked_task.get("topic_path"),
                    linked_improvement_id=str(improvement_id),
                    created_at=linked_task["created_at"],
                    updated_at=datetime.now(timezone.utc).timestamp(),
                )
                logger.info(
                    "Synced status from improvement %s to task %s: resolved → done",
                    improvement_id,
                    linked_task["id"],
                )
        except Exception as e:
            logger.warning(f"Failed to sync status to linked task for improvement {improvement_id}: {e}")

        return await self._get_improvement(key)

    async def _resolve_task(
        self,
        key: ArtifactKey,
        request: UnifiedArtifactResolveRequest,
    ) -> UnifiedArtifactRecord:
        """Разрешить task и синхронизировать с improvement."""
        # Получить task
        row = self._tasks_store.get_task_by_task_id(
            project=key.project,
            task_id=key.local_id,
        )
        if not row:
            raise ValueError(f"Task not found: {key.local_id}")

        # Обновить task статус на done
        self._tasks_store.upsert_task(
            memory_id=str(row["id"]),
            task_id=row["task_id"],
            project=row["project"],
            title=row["title"],
            description=row["description"],
            agent_id=row["agent_id"],
            status="done",
            source=row["source"],
            tags=row.get("tags") or [],
            topic_path=row.get("topic_path"),
            linked_improvement_id=row.get("linked_improvement_id"),
            created_at=row["created_at"],
            updated_at=datetime.now(timezone.utc).timestamp(),
        )

        # Синхронизировать с improvement, если есть связь
        if row.get("linked_improvement_id"):
            linked_improvement_id_str = row["linked_improvement_id"]
            try:
                linked_improvement_id = UUID(linked_improvement_id_str)
                project = await self._improvements_store.resolve(
                    linked_improvement_id,
                    acted_by=request.acted_by,
                    action_source=request.action_source,
                    reason=f"Task {key.local_id} marked as done. {request.reason}",
                )
                logger.info(f"Synced status from task {key.local_id} to improvement {linked_improvement_id}: done → resolved")
            except Exception as e:
                logger.warning(f"Failed to sync status to improvement {linked_improvement_id_str}: {e}")

        return await self._get_task(key)

    async def reopen_artifact(
        self,
        artifact_key: str,
        request: UnifiedArtifactReopenRequest,
    ) -> UnifiedArtifactRecord:
        """Переоткрыть сущность (improvement→open, task→active)."""
        key = ArtifactKey.parse(artifact_key)

        if key.type == "improvement":
            return await self._reopen_improvement(key, request)
        elif key.type == "task":
            return await self._reopen_task(key, request)
        else:
            raise ValueError(f"Unsupported artifact type: {key.type}")

    async def _reopen_improvement(
        self,
        key: ArtifactKey,
        request: UnifiedArtifactReopenRequest,
    ) -> UnifiedArtifactRecord:
        """Переоткрыть improvement и синхронизировать с task."""
        improvement_id = key.to_uuid()

        # Переоткрыть improvement с указанием причины
        project = await self._improvements_store.reopen(
            improvement_id,
            acted_by=request.acted_by,
            action_source=request.action_source if hasattr(request, 'action_source') else "unified_artifact",
            reason=request.reason,
        )

        if not project:
            raise ValueError(f"Improvement not found: {improvement_id}")

        # Получить обновленный improvement
        row = await self._improvements_store.get(improvement_id)
        if not row:
            raise ValueError(f"Improvement not found after reopen: {improvement_id}")

        # Синхронизировать с task, если есть связанная task запись
        try:
            linked_task = self._tasks_store.get_task_by_task_id(
                project=row["project"],
                task_id=str(improvement_id),
            )
            if linked_task:
                self._tasks_store.upsert_task(
                    memory_id=str(linked_task["id"]),
                    task_id=linked_task["task_id"],
                    project=linked_task["project"],
                    title=linked_task["title"],
                    description=linked_task["description"],
                    agent_id=linked_task["agent_id"],
                    status="active",
                    source=linked_task["source"],
                    tags=linked_task.get("tags") or [],
                    topic_path=linked_task.get("topic_path"),
                    linked_improvement_id=str(improvement_id),
                    created_at=linked_task["created_at"],
                    updated_at=datetime.now(timezone.utc).timestamp(),
                )
                logger.info(
                    "Synced status from improvement %s to task %s: open → active",
                    improvement_id,
                    linked_task["id"],
                )
        except Exception as e:
            logger.warning(f"Failed to sync status to linked task for improvement {improvement_id}: {e}")

        return await self._get_improvement(key)

    async def _reopen_task(
        self,
        key: ArtifactKey,
        request: UnifiedArtifactReopenRequest,
    ) -> UnifiedArtifactRecord:
        """Переоткрыть task и синхронизировать с improvement."""
        # Получить task
        row = self._tasks_store.get_task_by_task_id(
            project=key.project,
            task_id=key.local_id,
        )
        if not row:
            raise ValueError(f"Task not found: {key.local_id}")

        # Обновить task статус на active
        self._tasks_store.upsert_task(
            memory_id=str(row["id"]),
            task_id=row["task_id"],
            project=row["project"],
            title=row["title"],
            description=row["description"],
            agent_id=row["agent_id"],
            status="active",
            source=request.source,
            tags=row.get("tags") or [],
            topic_path=row.get("topic_path"),
            linked_improvement_id=row.get("linked_improvement_id"),
            created_at=row["created_at"],
            updated_at=datetime.now(timezone.utc).timestamp(),
        )

        # Синхронизировать с improvement, если есть связь
        if row.get("linked_improvement_id"):
            linked_improvement_id_str = row["linked_improvement_id"]
            try:
                linked_improvement_id = UUID(linked_improvement_id_str)
                imp_row = await self._improvements_store.get(linked_improvement_id)
                if imp_row:
                    await self._improvements_store.reopen(
                        linked_improvement_id,
                        acted_by=request.acted_by,
                        action_source=request.action_source,
                        reason=f"Task {key.local_id} reopened. {request.reason}",
                    )
                    logger.info(f"Synced status from task {key.local_id} to improvement {linked_improvement_id}: active → open")
            except Exception as e:
                logger.warning(f"Failed to sync status to improvement {linked_improvement_id_str}: {e}")

        return await self._get_task(key)


# Singleton
_service: Optional[UnifiedArtifactService] = None


def get_unified_artifact_service() -> UnifiedArtifactService:
    """Получить экземпляр UnifiedArtifactService."""
    global _service
    if _service is None:
        _service = UnifiedArtifactService()
    return _service
