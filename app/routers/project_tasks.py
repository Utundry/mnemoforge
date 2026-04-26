from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
from app.models.project_task import (
    ProjectTaskBackfillResponse,
    ProjectTaskChangeCreate,
    ProjectTaskChangeRecord,
    TaskCaptureCompletionResponse,
    TaskCaptureCandidateListResponse,
    TaskCaptureRejectRequest,
    TaskCaptureRejectResponse,
    TaskCapturePromoteRequest,
    TaskCapturePromoteResponse,
    ProjectTaskCreate,
    ProjectTaskReopenRequest,
    ProjectTaskRecord,
    TaskStatementProjectionResponse,
)
from app.services.project_task_service import (
    add_task_change,
    backfill_tasks_from_improvements,
    create_or_update_project_task,
    get_project_task,
    list_task_changes,
    reopen_project_task,
)
from app.services.task_capture_service import build_task_capture_completion
from app.services.task_capture_review_service import (
    list_task_capture_candidates,
    reject_task_capture_candidates,
    promote_task_capture_candidates,
)
from app.services.task_statement_service import build_task_statement_projection

router = APIRouter(prefix="/project/tasks", tags=["project-tasks"])


async def _enqueue_task_capture_refresh(
    queue,
    *,
    project: str,
    task_id: str,
    trigger: str,
    use_local_generation: bool,
) -> None:
    pending = queue.list_jobs(job_type="task_capture_refresh", limit=50)
    for job in pending:
        payload = job.get("payload") or {}
        if (
            str(payload.get("project") or "").strip() == project
            and str(payload.get("task_id") or "").strip() == task_id
            and str(job.get("status") or "").strip() in {"queued", "running"}
            and str(payload.get("trigger") or "") == trigger
        ):
            return
    await queue.submit(
        "task_capture_refresh",
        {
            "project": project,
            "task_id": task_id,
            "trigger": trigger,
            "use_local_generation": use_local_generation,
            "_queue_lane": "fast",
        },
    )


class ProjectTasksRebuildRequest(BaseModel):
    project: Optional[str] = Field(None, min_length=1, max_length=128)
    limit: int = Field(0, ge=0)
    changes_limit: int = Field(0, ge=0)


def _task_http_error(exc: Exception) -> HTTPException:
    status_code = 404 if str(exc) == "Task not found" else 400
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectTaskRecord)
async def create_task(body: ProjectTaskCreate, qdrant: QdrantDep, ollama: OllamaDep, queue: JobQueueDep):
    try:
        record = await create_or_update_project_task(qdrant, ollama, body)
        await _enqueue_task_capture_refresh(
            queue,
            project=record.project,
            task_id=record.task_id,
            trigger="task_created",
            use_local_generation=record.status in {"active", "done"},
        )
        return record
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.get("/{task_id}", response_model=ProjectTaskRecord)
async def get_task(
    task_id: str,
    qdrant: QdrantDep,
    project: str = Query(..., min_length=1, max_length=128),
):
    """Get a single task by task_id and project."""
    try:
        return await get_project_task(qdrant, project=project, task_id=task_id, include_changes=True)
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.post("/{task_id}/reopen", response_model=ProjectTaskRecord)
async def reopen_task(
    task_id: str,
    body: ProjectTaskReopenRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    queue: JobQueueDep,
):
    """Reopen a task to active or paused status."""
    try:
        record = await reopen_project_task(qdrant, ollama, task_id=task_id, body=body)
        await _enqueue_task_capture_refresh(
            queue,
            project=record.project,
            task_id=record.task_id,
            trigger=f"task_reopened:{record.status}",
            use_local_generation=record.status in {"active", "done"},
        )
        return record
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.post("/{task_id}/changes", status_code=status.HTTP_201_CREATED, response_model=ProjectTaskChangeRecord)
async def create_task_change(
    task_id: str,
    body: ProjectTaskChangeCreate,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    queue: JobQueueDep,
):
    try:
        record = await add_task_change(qdrant, ollama, task_id=task_id, body=body)
        tags = {str(tag).strip() for tag in (record.tags or []) if str(tag).strip()}
        await _enqueue_task_capture_refresh(
            queue,
            project=record.project,
            task_id=record.task_id,
            trigger=f"task_change:{record.change_type}",
            use_local_generation=record.change_type in {"decision", "implementation", "status_change"} or "task_checkpoint" in tags,
        )
        return record
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.get("/{task_id}/changes", response_model=list[ProjectTaskChangeRecord])
async def get_task_changes(
    task_id: str,
    qdrant: QdrantDep,
    project: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(100, ge=1, le=500),
):
    return await list_task_changes(qdrant, project=project, task_id=task_id, limit=limit)


@router.get("/{task_id}/statement", response_model=TaskStatementProjectionResponse)
async def get_task_statement(
    task_id: str,
    qdrant: QdrantDep,
    project: str = Query(..., min_length=1, max_length=128),
):
    try:
        return await build_task_statement_projection(qdrant, project=project, task_id=task_id)
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.post("/{task_id}/capture-candidates", response_model=TaskCaptureCompletionResponse)
async def create_task_capture_candidates(
    task_id: str,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    project: str = Query(..., min_length=1, max_length=128),
    persist: bool = Query(True),
    use_local_generation: bool = Query(True),
):
    try:
        return await build_task_capture_completion(
            qdrant,
            ollama,
            project=project,
            task_id=task_id,
            persist=persist,
            use_local_generation=use_local_generation,
        )
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.get("/{task_id}/capture-candidates", response_model=TaskCaptureCandidateListResponse)
async def get_task_capture_candidates(
    task_id: str,
    project: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        return await list_task_capture_candidates(project=project, task_id=task_id, limit=limit)
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.post("/{task_id}/capture-candidates/promote", response_model=TaskCapturePromoteResponse)
async def promote_capture_candidates(
    task_id: str,
    body: TaskCapturePromoteRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    project: str = Query(..., min_length=1, max_length=128),
):
    try:
        return await promote_task_capture_candidates(
            qdrant,
            ollama,
            project=project,
            task_id=task_id,
            artifact_ids=body.artifact_ids,
            acted_by=body.acted_by,
            review_source=body.review_source,
            reason=body.reason,
        )
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.post("/{task_id}/capture-candidates/reject", response_model=TaskCaptureRejectResponse)
async def reject_capture_candidates(
    task_id: str,
    body: TaskCaptureRejectRequest,
    qdrant: QdrantDep,
    project: str = Query(..., min_length=1, max_length=128),
):
    try:
        return await reject_task_capture_candidates(
            qdrant,
            project=project,
            task_id=task_id,
            artifact_ids=body.artifact_ids,
            acted_by=body.acted_by,
            review_source=body.review_source,
            reason=body.reason,
        )
    except Exception as exc:
        raise _task_http_error(exc) from exc


@router.post("/rebuild", status_code=status.HTTP_202_ACCEPTED)
async def rebuild_project_tasks_job(
    body: ProjectTasksRebuildRequest,
    queue: JobQueueDep,
):
    payload = {
        "project": body.project,
        "limit": body.limit,
        "changes_limit": body.changes_limit,
    }
    job_id = await queue.submit("rebuild_project_tasks", payload)
    return {"job_id": job_id, "job_type": "rebuild_project_tasks"}


@router.post("/backfill-from-improvements", response_model=ProjectTaskBackfillResponse)
async def backfill_from_improvements(
    qdrant: QdrantDep,
    ollama: OllamaDep,
    project: Optional[str] = Query(None, min_length=1, max_length=128),
    limit: int = Query(500, ge=1, le=5000),
):
    return await backfill_tasks_from_improvements(qdrant, ollama, project=project, limit=limit)
