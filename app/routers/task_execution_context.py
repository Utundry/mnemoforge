from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.dependencies import QdrantDep
from app.models.task_execution_context import TaskExecutionContextRequest, TaskExecutionContextResponse
from app.services.task_execution_context_service import build_task_execution_context

router = APIRouter(prefix="/task-execution-context", tags=["task-execution-context"])


@router.post("", response_model=TaskExecutionContextResponse)
async def get_task_execution_context(body: TaskExecutionContextRequest, qdrant: QdrantDep):
    try:
        return await build_task_execution_context(qdrant, body)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to build task execution context: {exc}",
        ) from exc
