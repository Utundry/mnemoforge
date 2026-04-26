from __future__ import annotations

import json
from uuid import UUID

from app.models.project_task import (
    ProjectTaskCreate,
    ProjectTaskChangeCreate,
    TaskCaptureCandidateListResponse,
    TaskCaptureCandidateRecord,
    TaskCaptureRejectResponse,
    TaskCapturePromoteResponse,
)
from app.services.learning_store import get_learning_store
from app.services.project_task_service import add_task_change, create_or_update_project_task, get_project_task
from app.services.text_localization import normalize_text_for_display


def _clean(value: str, limit: int = 1000) -> str:
    return normalize_text_for_display(str(value or ""))[:limit].strip()


def _extract_tag_value(tags: set[str], prefix: str) -> str:
    for tag in tags:
        if tag.startswith(prefix):
            return tag.split(":", 1)[1].strip()
    return ""


def _candidate_record_from_row(row: dict) -> TaskCaptureCandidateRecord:
    tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
    return TaskCaptureCandidateRecord(
        kind=_extract_tag_value(tags, "capture_kind:") or "draft",
        content=_clean(str(row.get("content") or ""), limit=4000),
        source=_extract_tag_value(tags, "capture_source:") or "",
        confidence=float(row.get("confidence") or 0.0),
        rationale=_clean(str(row.get("observation") or ""), limit=1000),
        artifact_id=str(row.get("id") or ""),
        artifact_type=str(row.get("artifact_type") or "task_capture_candidate"),
        reused_existing=False,
    )


async def list_task_capture_candidates(*, project: str, task_id: str, limit: int = 50) -> TaskCaptureCandidateListResponse:
    rows: list[dict] = []
    store = get_learning_store()
    for artifact_type in [
        "task_capture_candidate",
        "decision_candidate",
        "chosen_decision",
        "code_link",
        "remaining_risk",
    ]:
        rows.extend(
            await store.list_artifacts(
                artifact_type=artifact_type,
                scope="project",
                status="active",
                limit=max(limit * 4, 100),
            )
        )
    project_tag = f"project:{project}"
    task_tag = f"task_id:{task_id}"
    items: list[TaskCaptureCandidateRecord] = []
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if project_tag not in tags or task_tag not in tags:
            continue
        items.append(_candidate_record_from_row(row))
        if len(items) >= limit:
            break
    return TaskCaptureCandidateListResponse(project=project, task_id=task_id, found=len(items), candidates=items)


def _append_labeled_line(description: str, *, label: str, value: str) -> str:
    normalized = normalize_text_for_display(description or "")
    candidate_line = f"{label}: {value}".strip()
    lower_line = candidate_line.casefold()
    existing_lines = [normalize_text_for_display(line).strip() for line in normalized.splitlines()]
    if any(line.casefold() == lower_line for line in existing_lines if line):
        return normalized
    if normalized:
        return f"{normalized}\n{candidate_line}".strip()
    return candidate_line


def _promotion_change_payload(*, kind: str, content: str, reason: str, acted_by: str, project: str) -> ProjectTaskChangeCreate:
    if kind == "chosen_decision":
        return ProjectTaskChangeCreate(
            project=project,
            change_type="decision",
            content=content,
            why=f"Promoted from task capture ({kind}). {reason}".strip(),
            agent_id=acted_by,
            source="task_capture_promotion",
            tags=["task-capture-promotion", f"capture_kind:{kind}"],
        )
    if kind == "code_link":
        return ProjectTaskChangeCreate(
            project=project,
            change_type="implementation",
            content=f"Code link: {content}",
            why=f"Promoted code traceability link. {reason}".strip(),
            agent_id=acted_by,
            source="task_capture_promotion",
            tags=["task-capture-promotion", f"capture_kind:{kind}"],
        )
    if kind == "remaining_risk":
        return ProjectTaskChangeCreate(
            project=project,
            change_type="note",
            content=f"Remaining risk: {content}",
            why=f"Promoted post-completion risk. {reason}".strip(),
            agent_id=acted_by,
            source="task_capture_promotion",
            tags=["task-capture-promotion", f"capture_kind:{kind}"],
        )
    return ProjectTaskChangeCreate(
        project=project,
        change_type="note",
        content=content,
        why=f"Promoted from task_capture_candidate ({kind}). {reason}".strip(),
        agent_id=acted_by,
        source="task_capture_promotion",
        tags=["task-capture-promotion", f"capture_kind:{kind}"],
    )


async def promote_task_capture_candidates(
    qdrant,
    ollama,
    *,
    project: str,
    task_id: str,
    artifact_ids: list[str],
    acted_by: str,
    review_source: str,
    reason: str,
) -> TaskCapturePromoteResponse:
    task = await get_project_task(qdrant, project=project, task_id=task_id, include_changes=False)
    store = get_learning_store()
    promoted: list[str] = []
    skipped: list[str] = []
    archived_count = 0
    description = task.description
    description_changed = False

    for artifact_id in artifact_ids:
        try:
            row = await store.get_artifact(UUID(str(artifact_id)))
        except Exception:
            row = None
        if not row:
            skipped.append(str(artifact_id))
            continue
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if f"project:{project}" not in tags or f"task_id:{task_id}" not in tags:
            skipped.append(str(artifact_id))
            continue
        kind = _extract_tag_value(tags, "capture_kind:") or "draft"
        content = _clean(str(row.get("content") or ""), limit=4000)
        if not content:
            skipped.append(str(artifact_id))
            continue

        if kind in {"assumption", "constraint", "definition_of_done"}:
            label = {
                "assumption": "Assumption",
                "constraint": "Constraint",
                "definition_of_done": "Definition of done",
            }[kind]
            updated = _append_labeled_line(description, label=label, value=content)
            if updated != description:
                description = updated
                description_changed = True
            promoted.append(str(artifact_id))
        elif kind == "decision_candidate":
            updated = _append_labeled_line(description, label="Decision candidate", value=content)
            if updated != description:
                description = updated
                description_changed = True
            promoted.append(str(artifact_id))
        elif kind in {"chosen_decision", "code_link", "remaining_risk", "verification_result", "result_summary", "handoff_summary"}:
            await add_task_change(
                qdrant,
                ollama,
                task_id=task_id,
                body=_promotion_change_payload(
                    kind=kind,
                    content=content,
                    reason=reason,
                    acted_by=acted_by,
                    project=project,
                ),
            )
            promoted.append(str(artifact_id))
        else:
            skipped.append(str(artifact_id))
            continue

        archived = await store.set_artifact_status(
            UUID(str(artifact_id)),
            status="archived",
            acted_by=acted_by,
            action_source=review_source,
            reason=reason,
        )
        if archived is not None:
            archived_count += 1

    if description_changed:
        await create_or_update_project_task(
            qdrant,
            ollama,
            ProjectTaskCreate(
                task_id=task.task_id,
                project=task.project,
                title=task.title,
                description=description,
                agent_id=task.agent_id,
                status=task.status,
                source=task.source,
                tags=task.tags,
                topic_path=task.topic_path,
                linked_improvement_id=task.linked_improvement_id,
            ),
        )

    return TaskCapturePromoteResponse(
        project=project,
        task_id=task_id,
        promoted_count=len(promoted),
        archived_count=archived_count,
        skipped_count=len(skipped),
        promoted_artifact_ids=promoted,
        skipped_artifact_ids=skipped,
    )


async def reject_task_capture_candidates(
    qdrant,
    *,
    project: str,
    task_id: str,
    artifact_ids: list[str],
    acted_by: str,
    review_source: str,
    reason: str,
) -> TaskCaptureRejectResponse:
    await get_project_task(qdrant, project=project, task_id=task_id, include_changes=False)
    store = get_learning_store()
    rejected: list[str] = []
    skipped: list[str] = []
    archived_count = 0

    for artifact_id in artifact_ids:
        try:
            row = await store.get_artifact(UUID(str(artifact_id)))
        except Exception:
            row = None
        if not row:
            skipped.append(str(artifact_id))
            continue
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if f"project:{project}" not in tags or f"task_id:{task_id}" not in tags:
            skipped.append(str(artifact_id))
            continue
        archived = await store.set_artifact_status(
            UUID(str(artifact_id)),
            status="archived",
            acted_by=acted_by,
            action_source=review_source,
            reason=reason,
        )
        if archived is None:
            skipped.append(str(artifact_id))
            continue
        archived_count += 1
        rejected.append(str(artifact_id))

    return TaskCaptureRejectResponse(
        project=project,
        task_id=task_id,
        rejected_count=len(rejected),
        archived_count=archived_count,
        skipped_count=len(skipped),
        rejected_artifact_ids=rejected,
        skipped_artifact_ids=skipped,
    )
