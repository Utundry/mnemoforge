from __future__ import annotations

import logging
import re
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
from app.services.memory_store import get_memory_store
from app.services.project_identity_service import project_lookup_ids, resolve_project_id
from app.services.project_task_service import _task_capture_summary_map
from app.services.project_tasks_store import get_project_tasks_store
from app.services.mcp_workflow_specs import load_named_json_spec

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


def _artifact_lookup_spec() -> dict:
    try:
        return load_named_json_spec("search/artifact_lookup.json")
    except Exception as exc:
        logger.warning("Artifact lookup spec unavailable: %s", exc)
        return {}


def _query_tokens(query: str, spec: dict) -> list[str]:
    stop_terms = {str(term).casefold() for term in spec.get("stop_terms") or []}
    return [
        token
        for token in re.findall(r"[\w]+", str(query or "").casefold(), flags=re.UNICODE)
        if token and token not in stop_terms
    ]


def _artifact_text_parts(item: UnifiedArtifactRecord) -> dict[str, str]:
    tags = " ".join(
        str(tag or "").casefold().replace("#", "").replace("_", "-")
        for tag in item.tags or []
    )
    return {
        "title": str(item.title or "").casefold(),
        "description": str(item.description or "").casefold(),
        "tags": tags,
        "refs": " ".join(
            str(value or "")
            for value in (item.artifact_key, item.linked_artifact_key, item.task_id, item.topic_path)
        ).casefold(),
    }


def _query_alias_terms(query: str, spec: dict) -> set[str]:
    text = str(query or "").casefold()
    terms: set[str] = set()
    for group in spec.get("alias_groups") or []:
        if not isinstance(group, dict):
            continue
        triggers = [str(trigger or "").casefold() for trigger in group.get("triggers") or []]
        if any(trigger and trigger in text for trigger in triggers):
            terms.update(str(term or "").casefold() for term in group.get("terms") or [] if str(term or "").strip())
            terms.update(_normalize_topic_tag(term) for term in group.get("topic_tags") or [] if str(term or "").strip())
    return terms


def _query_topic_tags(query: str, spec: dict) -> list[str]:
    text = str(query or "").casefold()
    tags: list[str] = []
    for group in spec.get("alias_groups") or []:
        if not isinstance(group, dict):
            continue
        triggers = [str(trigger or "").casefold() for trigger in group.get("triggers") or []]
        if any(trigger and trigger in text for trigger in triggers):
            tags.extend(str(tag or "").strip() for tag in group.get("topic_tags") or [] if str(tag or "").strip())
    return list(dict.fromkeys(tags))


def _normalize_topic_tag(value: str) -> str:
    return str(value or "").strip().casefold().lstrip("#")


def _artifact_query_score(item: UnifiedArtifactRecord, query: str) -> float:
    spec = _artifact_lookup_spec()
    weights = spec.get("weights") if isinstance(spec.get("weights"), dict) else {}
    tokens = _query_tokens(query, spec)
    alias_terms = _query_alias_terms(query, spec)
    if not tokens and not alias_terms:
        return 1.0

    parts = _artifact_text_parts(item)
    haystack = " ".join(parts.values())
    score = 0.0
    exact_weight = float(weights.get("exact_token") or 4.0)
    alias_weight = float(weights.get("alias_token") or 1.5)
    title_multiplier = float(weights.get("title_multiplier") or 2.0)
    description_multiplier = float(weights.get("description_multiplier") or 1.0)
    tag_multiplier = float(weights.get("tag_multiplier") or 1.2)

    for token in tokens:
        if token in parts["title"]:
            score += exact_weight * title_multiplier
        elif token in parts["description"] or token in parts["refs"]:
            score += exact_weight * description_multiplier
        elif token in parts["tags"]:
            score += exact_weight * tag_multiplier

    for token in alias_terms:
        if token in parts["title"]:
            score += alias_weight * title_multiplier
        elif token in parts["description"] or token in parts["refs"]:
            score += alias_weight * description_multiplier
        elif token in parts["tags"]:
            score += alias_weight * tag_multiplier

    phrase = " ".join(tokens)
    if phrase and phrase in haystack:
        score += float(weights.get("phrase") or 7.0)

    status_weights = spec.get("status_weights") if isinstance(spec.get("status_weights"), dict) else {}
    type_weights = spec.get("type_weights") if isinstance(spec.get("type_weights"), dict) else {}
    score += float(status_weights.get(str(item.status or ""), 0.0) or 0.0)
    score += float(type_weights.get(str(item.type or ""), 0.0) or 0.0)

    diagnostic_terms = [str(term or "").casefold() for term in spec.get("diagnostic_penalty_terms") or []]
    if any(term and term in haystack for term in diagnostic_terms):
        score += float(weights.get("diagnostic_penalty") or -8.0)
    return score


def _artifact_query_match_reason(item: UnifiedArtifactRecord, query: str, score: float) -> tuple[str, list[str]]:
    spec = _artifact_lookup_spec()
    topic_tags = _query_topic_tags(query, spec)
    if topic_tags:
        return (
            f"Matched topic aliases {', '.join(topic_tags[:4])}; ranked by subject relevance, status, and diagnostic-noise penalty.",
            topic_tags,
        )
    tokens = _query_tokens(query, spec)
    if tokens:
        return (f"Matched query terms: {', '.join(tokens[:5])}.", [])
    return (f"Ranked by artifact relevance score {score:.2f}.", [])


def _replace_task_status_tag(tags: list[str] | None, status: str) -> list[str]:
    cleaned = [
        str(tag).strip()
        for tag in (tags or [])
        if str(tag).strip() and not str(tag).strip().startswith("task_status:")
    ]
    status_tag = f"task_status:{status}"
    if status_tag not in cleaned:
        cleaned.append(status_tag)
    return cleaned


_TERMINAL_UNIFIED_STATUSES = {"done", "resolved", "completed", "closed", "cancelled", "archived"}
_OPEN_WORK_UNIFIED_STATUSES = {"open", "active"}
_LEGACY_CLOSEOUT_MARKERS = ("finish_task", "finished_by_mailbox", "record_work_result closeout")


def task_has_closeout_evidence(changes: list[dict]) -> bool:
    for change in changes:
        tags = {str(tag or "").strip().lower() for tag in change.get("tags") or []}
        content = str(change.get("content") or "").casefold()
        why = str(change.get("why") or "").casefold()
        source = str(change.get("source") or "").casefold()
        if "task_status:done" in tags or "task_stage:completed" in tags:
            return True
        if "checkpoint status: done" in content or "checkpoint stage: completed" in content:
            return True
        closeout_text = " ".join((content, why, source))
        if any(marker in closeout_text for marker in _LEGACY_CLOSEOUT_MARKERS):
            return True
    return False




def _status_matches_request(item_status: str, requested_status: str | None) -> bool:
    if not requested_status:
        return True
    status = str(item_status or "").strip().lower()
    requested = str(requested_status or "").strip().lower()
    if requested == "open":
        return status in _OPEN_WORK_UNIFIED_STATUSES
    return status == requested


def _artifact_work_priority(item: UnifiedArtifactRecord) -> float:
    if item.importance_score is not None:
        try:
            return float(item.importance_score)
        except (TypeError, ValueError):
            pass
    tags = {str(tag).strip().casefold() for tag in (item.tags or []) if str(tag).strip()}
    if tags & {"priority:critical", "critical"}:
        return 1.0
    if tags & {"priority:high", "high_priority"}:
        return 0.9
    if tags & {"priority:low", "low_priority"}:
        return 0.4
    return 0.7


class UnifiedArtifactService:
    """Единый фасад для доступа к improvements и tasks."""

    def __init__(self):
        self._improvements_store = get_improvements_store()
        self._tasks_store = get_project_tasks_store()

    async def _get_semantic_candidate(self, artifact_key: str) -> UnifiedArtifactRecord:
        parts = str(artifact_key or "").split(":", 2)
        if len(parts) != 3 or parts[0] not in {"memory", "project_tree"}:
            return await self.get_artifact(artifact_key)
        requested_type, project, local_id = parts
        row = await get_memory_store().get(local_id)
        if not row:
            raise ValueError(f"Semantic candidate not found in SQLite: {local_id}")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        canonical_project = resolve_project_id(project)
        stored_project = str(metadata.get("project") or metadata.get("project_id") or "").strip()
        if stored_project and stored_project not in project_lookup_ids(canonical_project):
            raise ValueError(f"Semantic candidate belongs to another project: {local_id}")
        type_ = "project_tree" if str(row.get("category") or "") == "doc_section" else "memory"
        if requested_type == "project_tree" and type_ != requested_type:
            logger.debug("Qdrant candidate type was stale for memory %s; SQLite category won.", local_id)
        content = str(row.get("content") or "").strip()
        title = str(metadata.get("title") or "").strip() or content.splitlines()[0][:256]
        created_at = datetime.fromtimestamp(float(row.get("created_at") or 0.0), tz=timezone.utc)
        updated_at = datetime.fromtimestamp(float(row.get("updated_at") or row.get("created_at") or 0.0), tz=timezone.utc)
        return UnifiedArtifactRecord(
            artifact_key=f"{type_}:{canonical_project}:{local_id}",
            type=type_,
            id=UUID(local_id),
            project=canonical_project,
            title=title or local_id,
            description=content,
            status=str(metadata.get("status") or "active"),
            agent_id=str(metadata.get("agent_id") or "unknown"),
            tags=list(metadata.get("tags") or []),
            created_at=created_at,
            updated_at=updated_at,
            source=str(metadata.get("source") or row.get("category") or ""),
            topic_path=metadata.get("topic_path"),
        )

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
        linked_closeout_evidence = False
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
                linked_changes = self._tasks_store.list_changes(
                    project=str(linked_task.get("project") or canonical_project),
                    task_id=str(linked_task["task_id"]),
                    limit=500,
                )
                linked_closeout_evidence = task_has_closeout_evidence(linked_changes)
        except Exception as e:
            logger.warning(f"Failed to get linked task for improvement {improvement_id}: {e}")
        status = to_unified_status("improvement", row["status"])
        if status == "open" and (
            str(linked_status or "").strip().lower() in _TERMINAL_UNIFIED_STATUSES
            or linked_closeout_evidence
        ):
            status = "done"

        return UnifiedArtifactRecord(
            artifact_key=str(ArtifactKey(type="improvement", project=canonical_project, local_id=key.local_id)),
            type="improvement",
            id=improvement_id,
            project=canonical_project,
            title=row["title"],
            description=row["description"],
            status=status,
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
            row = self._tasks_store.get_unique_task_by_uuid(task_id=key.local_id)

        if not row:
            raise ValueError(f"Task not found: {key.local_id}")

        # Получить связанный improvement, если есть
        stored_project = str(row.get("project") or canonical_project)
        summary = (await _task_capture_summary_map(stored_project, limit_hint=1)).get(key.local_id) or {}
        changes = self._tasks_store.list_changes(project=stored_project, task_id=key.local_id, limit=500)

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
        status = to_unified_status("task", row["status"])
        if status == "open" and (
            str(linked_status or "").strip().lower() in _TERMINAL_UNIFIED_STATUSES
            or task_has_closeout_evidence(changes)
        ):
            status = "done"

        return UnifiedArtifactRecord(
            artifact_key=str(ArtifactKey(type="task", project=canonical_project, local_id=key.local_id)),
            type="task",
            id=UUID(row["id"]),
            project=canonical_project,
            title=row["title"],
            description=row["description"],
            status=status,
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
        semantic_candidates: dict[str, float] | None = None,
    ) -> UnifiedArtifactListResponse:
        """Получить список сущностей с фильтрацией по типу и статусу."""
        canonical_project = resolve_project_id(project)
        lookup_projects = project_lookup_ids(canonical_project)
        items: list[UnifiedArtifactRecord] = []
        requested_status = str(status or "").strip().lower()
        open_work_request = requested_status == "open"
        fetch_limit = max(limit, 100) if query or open_work_request else limit

        if semantic_candidates is not None:
            for artifact_key, score in semantic_candidates.items():
                try:
                    item = await self._get_semantic_candidate(artifact_key)
                except (TypeError, ValueError):
                    continue
                if type_ and item.type != type_:
                    continue
                item.query_score = round(float(score), 6)
                item.match_reason = (
                    "Semantic candidate from Qdrant; authoritative artifact rehydrated and validated from SQLite."
                )
                items.append(item)

        # Convert the requested unified status into source-specific status families.
        improvement_statuses: list[str | None] = [None]
        task_statuses: list[str | None] = [None]
        if status:
            if open_work_request:
                improvement_statuses = [from_unified_status("improvement", "open")]
                task_statuses = ["planning", "active"]
            else:
                improvement_statuses = [from_unified_status("improvement", status)]
                task_statuses = [from_unified_status("task", status)]

        # Получить improvements
        if semantic_candidates is None and (type_ is None or type_ == "improvement"):
            improvements = []
            for lookup_project in lookup_projects:
                for improvement_status in improvement_statuses:
                    improvements.extend(
                        await self._improvements_store.list(
                            project=lookup_project,
                            status=improvement_status,
                            limit=fetch_limit,
                        )
                    )
            for imp in improvements:
                try:
                    key = ArtifactKey(type="improvement", project=canonical_project, local_id=str(imp["id"]))
                    items.append(await self._get_improvement(key))
                except Exception as e:
                    logger.warning(f"Failed to convert improvement {imp['id']}: {e}")

        # Получить tasks
        if semantic_candidates is None and (type_ is None or type_ == "task"):
            tasks = []
            for lookup_project in lookup_projects:
                for task_status in task_statuses:
                    tasks.extend(
                        self._tasks_store.list_tasks(
                            project=lookup_project,
                            status=task_status,
                            limit=fetch_limit,
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
            items = [item for item in items if _status_matches_request(item.status, status)]
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
        if query and semantic_candidates is None:
            scored_items = [(item, _artifact_query_score(item, query)) for item in items]
            scored_items = [(item, score) for item, score in scored_items if score > 0]
            scored_items.sort(key=lambda pair: (pair[1], pair[0].updated_at.timestamp()), reverse=True)
            for item, score in scored_items:
                reason, topic_tags = _artifact_query_match_reason(item, query, score)
                item.query_score = round(float(score), 3)
                item.match_reason = reason
                item.matched_topic_tags = topic_tags
            items = [item for item, _score in scored_items]

        # Сортировать по updated_at (новые первыми)
        if semantic_candidates is not None:
            items.sort(
                key=lambda item: (float(item.query_score or 0.0), item.updated_at.timestamp()),
                reverse=True,
            )
        elif not query:
            if open_work_request:
                items.sort(
                    key=lambda item: (_artifact_work_priority(item), item.updated_at.timestamp()),
                    reverse=True,
                )
            else:
                items.sort(key=lambda x: x.updated_at, reverse=True)

        # Ограничить количество
        items = items[:limit]

        return UnifiedArtifactListResponse(
            total=len(items),
            items=items,
            search_mode="semantic" if semantic_candidates is not None else "lexical",
            backend_used="qdrant_candidates_sqlite_authority" if semantic_candidates is not None else "sqlite_lexical",
            candidate_count=len(semantic_candidates or {}),
            sqlite_validated_count=len(items) if semantic_candidates is not None else 0,
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
            linked_task = None
            for lookup_project in project_lookup_ids(resolve_project_id(row["project"])):
                linked_task = self._tasks_store.get_task_by_task_id(
                    project=lookup_project,
                    task_id=str(improvement_id),
                )
                if linked_task:
                    break
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
                    tags=_replace_task_status_tag(linked_task.get("tags") or [], "done"),
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
        canonical_project = resolve_project_id(key.project)
        row = None
        for lookup_project in project_lookup_ids(canonical_project):
            row = self._tasks_store.get_task_by_task_id(
                project=lookup_project,
                task_id=key.local_id,
            )
            if row:
                break
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
            tags=_replace_task_status_tag(row.get("tags") or [], "done"),
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
