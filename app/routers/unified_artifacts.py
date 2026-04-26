from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status as http_status

from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
from app.models.unified_artifact import (
    UnifiedArtifactListResponse,
    UnifiedArtifactRecord,
    UnifiedArtifactReopenRequest,
    UnifiedArtifactResolveRequest,
)
from app.services.unified_artifact_service import get_unified_artifact_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifacts", tags=["unified-artifacts"])


async def _sync_resolved_to_tree_node(node_id: str) -> None:
    """Mark the linked tree node as done when improvement is resolved."""
    try:
        from app.services.project_tree_store import get_tree_store
        ts = get_tree_store()
        node = ts.get_node(node_id)
        if node and node.get("status") != "done":
            meta = node.get("meta_json") or {}
            meta["org_last_action_type"] = "resolve_improvement_sync"
            meta["org_last_action_by"] = "system"
            meta["org_last_action_source"] = "linked_improvement_resolution"
            meta["org_last_action_at"] = time.time()
            meta["org_last_action_reason"] = "Linked improvement resolved"
            ts.update_node(node_id, status="done", meta_json=meta)
            logger.info("Improvement resolved → node %s marked done", node_id)
    except Exception as e:
        logger.warning("Failed to sync resolved improvement to tree: %s", e)


async def _run_best_effort_improvement_post_resolve(
    *,
    artifact_key: str,
    project: str,
    improvement_row: dict,
    request,
    queue: JobQueueDep,
    background_tasks: BackgroundTasks,
    qdrant: QdrantDep,
    ollama: OllamaDep,
) -> None:
    """Run improvement follow-up work without failing the main resolve."""
    from app.models.unified_artifact import ArtifactKey
    from app.services.project_task_service import ensure_task_for_improvement, record_improvement_task_change

    needs_repair = False

    try:
        await ensure_task_for_improvement(qdrant, ollama, improvement_row)
    except Exception as e:
        logger.warning("Failed to ensure task for resolved improvement %s: %s", artifact_key, e)
        needs_repair = True

    try:
        await record_improvement_task_change(
            qdrant,
            ollama,
            improvement_row=improvement_row,
            change_type="status_change",
            content="Improvement marked resolved.",
            why=request.reason or "Resolution was explicitly recorded.",
            source=request.action_source or "inline_user_approval",
        )
    except Exception as e:
        logger.warning("Failed to record task change for resolved improvement %s: %s", artifact_key, e)
        needs_repair = True

    try:
        await queue.submit(
            "task_memoir",
            {
                "task_id": str(ArtifactKey.parse(artifact_key).to_uuid()),
                "project": project,
            },
        )
    except Exception as e:
        logger.warning("Failed to submit task_memoir job for %s: %s", artifact_key, e)
        needs_repair = True

    try:
        await queue.submit("docs_rebuild", {"project": project})
    except Exception as e:
        logger.warning("Failed to submit docs_rebuild job for %s: %s", artifact_key, e)
        needs_repair = True

    if needs_repair:
        try:
            await queue.submit(
                "rebuild_project_tasks",
                {
                    "project": project,
                    "_queue_lane": "slow",
                },
            )
        except Exception as e:
            logger.warning("Failed to submit rebuild_project_tasks job for %s: %s", artifact_key, e)

    node_id = improvement_row.get("node_id", "")
    if node_id:
        background_tasks.add_task(_sync_resolved_to_tree_node, node_id)


@router.get("", response_model=UnifiedArtifactListResponse)
async def list_artifacts(
    project: str = Query("supermemory", description="Project name"),
    status: Optional[str] = Query(None, description="Filter by status (open, done, etc.)"),
    artifact_status: Optional[str] = Query(None, description="Deprecated alias for status"),
    type: Optional[str] = Query(None, description="Filter by type (improvement, task, or null for both)"),
    created_after: Optional[datetime] = Query(None, description="Filter by created_at >= timestamp"),
    created_before: Optional[datetime] = Query(None, description="Filter by created_at <= timestamp"),
    updated_after: Optional[datetime] = Query(None, description="Filter by updated_at >= timestamp"),
    updated_before: Optional[datetime] = Query(None, description="Filter by updated_at <= timestamp"),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of results"),
):
    """List unified artifacts with optional filtering.

    Args:
        project: Project name (default: supermemory)
        artifact_status: Filter by status (open, done, paused, archived)
        type: Filter by type (improvement, task, or null for both)
        limit: Maximum number of results (1-100)

    Returns:
        UnifiedArtifactListResponse with list of artifacts
    """
    try:
        service = get_unified_artifact_service()
        result = await service.list_artifacts(
            project=project,
            status=status or artifact_status,
            type_=type,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            limit=limit,
        )
        return result
    except Exception as e:
        logger.error(f"Error listing artifacts: {e}")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list artifacts: {str(e)}",
        )


@router.get("/{artifact_key}", response_model=UnifiedArtifactRecord)
async def get_artifact(artifact_key: str):
    """Get a unified artifact by artifact_key.

    Args:
        artifact_key: Artifact key in format: {type}:{project}:{local_id}
            - Example: improvement:supermemory:2e8fdc03-fc0b-4f77-bbaa-99f570e8894c
            - Example: task:supermemory:6174ad7b-1fd9-4b6b-bb59-4f932b8cfc8c

    Returns:
        UnifiedArtifactRecord with artifact details

    Raises:
        HTTPException 404: If artifact not found
        HTTPException 400: If artifact_key format is invalid
    """
    try:
        service = get_unified_artifact_service()
        result = await service.get_artifact(artifact_key)
        return result
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error getting artifact {artifact_key}: {e}")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get artifact: {str(e)}",
        )


@router.post("/{artifact_key}/resolve", response_model=UnifiedArtifactRecord)
async def resolve_artifact(
    artifact_key: str,
    request: UnifiedArtifactResolveRequest,
    queue: JobQueueDep,
    background_tasks: BackgroundTasks,
    qdrant: QdrantDep,
    ollama: OllamaDep,
):
    """Resolve a unified artifact (improvement→resolved, task→done).

    Args:
        artifact_key: Artifact key in format: {type}:{project}:{local_id}
        request: Resolve request with acted_by, action_source, reason
        queue: Job queue for background tasks
        background_tasks: Background tasks manager
        qdrant: Qdrant service
        ollama: Ollama service

    Returns:
        UnifiedArtifactRecord with updated artifact

    Raises:
        HTTPException 404: If artifact not found
        HTTPException 400: If artifact_key format is invalid
    """
    try:
        from app.services.improvements_store import get_improvements_store
        from app.models.unified_artifact import ArtifactKey
        
        key = ArtifactKey.parse(artifact_key)
        
        # For improvements, we need to perform additional actions
        if key.type == "improvement":
            # Get the improvement before resolving
            store = get_improvements_store()
            row = await store.get(key.to_uuid())
            if not row:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail=f"Improvement not found: {artifact_key}"
                )
            
            # Resolve through service
            service = get_unified_artifact_service()
            result = await service.resolve_artifact(artifact_key, request)

            # Get updated improvement
            updated = await store.get(key.to_uuid())
            if updated:
                await _run_best_effort_improvement_post_resolve(
                    artifact_key=artifact_key,
                    project=key.project,
                    improvement_row=updated,
                    request=request,
                    queue=queue,
                    background_tasks=background_tasks,
                    qdrant=qdrant,
                    ollama=ollama,
                )

            return result
        else:
            # For tasks, just resolve through service
            service = get_unified_artifact_service()
            result = await service.resolve_artifact(artifact_key, request)
            return result
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error resolving artifact {artifact_key}: {e}")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resolve artifact: {str(e)}",
        )


@router.post("/{artifact_key}/reopen", response_model=UnifiedArtifactRecord)
async def reopen_artifact(
    artifact_key: str,
    request: UnifiedArtifactReopenRequest,
):
    """Reopen a unified artifact (improvement→open, task→active).

    Args:
        artifact_key: Artifact key in format: {type}:{project}:{local_id}
        request: Reopen request with project, status, reason, acted_by, source

    Returns:
        UnifiedArtifactRecord with updated artifact

    Raises:
        HTTPException 404: If artifact not found
        HTTPException 400: If artifact_key format is invalid
    """
    try:
        service = get_unified_artifact_service()
        result = await service.reopen_artifact(artifact_key, request)
        return result
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error reopening artifact {artifact_key}: {e}")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reopen artifact: {str(e)}",
        )
