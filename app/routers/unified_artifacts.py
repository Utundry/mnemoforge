from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status as http_status
from qdrant_client.http import models as qmodels

from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
from app.models.unified_artifact import (
    UnifiedArtifactListResponse,
    UnifiedArtifactRecord,
    UnifiedArtifactReopenRequest,
    UnifiedArtifactResolveRequest,
)
from app.models.artifact_lifecycle import (
    ArtifactLifecycleReconcileRequest,
    ArtifactLifecycleReconcileResponse,
    ArtifactLifecycleScopeReviewBatchRequest,
    ArtifactLifecycleScopeReviewBatchResponse,
    LifecycleAnomalyRepairResponse,
    ArtifactLifecycleScopeReviewRequest,
    ArtifactLifecycleScopeReviewResponse,
)
from app.models.project_task import ProjectTaskChangeCreate
from app.services.artifact_lifecycle_service import (
    build_checkpoint_scope_review_content,
    reconcile_completed_checkpoint_artifacts,
    list_completed_but_open_anomalies,
)
from app.services.embedding_gateway import embed_query
from app.services.project_identity_service import project_lookup_ids, resolve_project_id
from app.services.project_task_service import add_task_change
from app.services.unified_artifact_service import get_unified_artifact_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifacts", tags=["unified-artifacts"])


def _semantic_candidate_keys(hit, *, public_project: str) -> list[str]:
    payload = dict(getattr(hit, "payload", None) or {})
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    tags = [str(tag or "") for tag in payload.get("tags") or []]
    task_id = str(meta.get("task_id") or "").strip()
    if not task_id:
        task_tag = next((tag for tag in tags if tag.startswith("task_id:")), "")
        task_id = task_tag.split(":", 1)[1].strip() if task_tag else ""
    improvement_id = str(
        meta.get("linked_improvement_id")
        or meta.get("improvement_id")
        or ""
    ).strip()
    keys: list[str] = []
    if task_id:
        keys.append(f"task:{public_project}:{task_id}")
    if improvement_id:
        keys.append(f"improvement:{public_project}:{improvement_id}")
    if not keys:
        category = str(payload.get("category") or "").strip()
        candidate_type = "project_tree" if category == "doc_section" else "memory"
        keys.append(f"{candidate_type}:{public_project}:{hit.id}")
    return keys


async def _semantic_artifact_candidates(
    *,
    qdrant,
    ollama,
    project: str,
    query: str,
    limit: int,
) -> dict[str, float]:
    vector, _embedding_meta = await embed_query(
        query,
        primary=ollama,
        purpose="semantic_artifact_lookup",
    )
    canonical_project = resolve_project_id(project)
    scores: dict[str, float] = {}
    for lookup_project in project_lookup_ids(canonical_project):
        hits = await qdrant._client.search(
            collection_name=qdrant._collection,
            query_vector=vector,
            query_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="project",
                        match=qmodels.MatchValue(value=lookup_project),
                    )
                ]
            ),
            limit=max(limit * 4, 40),
            with_payload=True,
        )
        for hit in hits:
            for artifact_key in _semantic_candidate_keys(hit, public_project=canonical_project):
                scores[artifact_key] = max(scores.get(artifact_key, 0.0), float(hit.score))
    return scores


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
    qdrant: QdrantDep,
    ollama: OllamaDep,
    project: str = Query("mnemoforge", description="Project name"),
    status: Optional[str] = Query(None, description="Filter by status (open, done, etc.)"),
    artifact_status: Optional[str] = Query(None, description="Deprecated alias for status"),
    type: Optional[str] = Query(None, description="Filter by type (improvement, task, or null for both)"),
    query: Optional[str] = Query(None, description="Filter by title, description, topic, tags, or public refs"),
    created_after: Optional[datetime] = Query(None, description="Filter by created_at >= timestamp"),
    created_before: Optional[datetime] = Query(None, description="Filter by created_at <= timestamp"),
    updated_after: Optional[datetime] = Query(None, description="Filter by updated_at >= timestamp"),
    updated_before: Optional[datetime] = Query(None, description="Filter by updated_at <= timestamp"),
    search_mode: str = Query("lexical", pattern="^(lexical|semantic)$"),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of results"),
):
    """List unified artifacts with optional filtering.

    Args:
        project: Project name (default: mnemoforge)
        artifact_status: Filter by status (open, done, paused, archived)
        type: Filter by type (improvement, task, or null for both)
        limit: Maximum number of results (1-100)

    Returns:
        UnifiedArtifactListResponse with list of artifacts
    """
    try:
        service = get_unified_artifact_service()
        semantic_candidates = None
        if search_mode == "semantic":
            if not str(query or "").strip():
                raise ValueError("Semantic artifact lookup requires a non-empty query.")
            semantic_candidates = await _semantic_artifact_candidates(
                qdrant=qdrant,
                ollama=ollama,
                project=project,
                query=str(query),
                limit=limit,
            )
        result = await service.list_artifacts(
            project=project,
            status=status or artifact_status,
            type_=type,
            query=query,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            limit=limit,
            semantic_candidates=semantic_candidates,
        )
        return result
    except Exception as e:
        logger.error(f"Error listing artifacts: {e}")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list artifacts: {str(e)}",
        )


@router.post("/reconcile-completed-checkpoints", response_model=ArtifactLifecycleReconcileResponse)
async def reconcile_completed_checkpoints(body: ArtifactLifecycleReconcileRequest):
    """Find open artifacts whose latest strict completion checkpoint says they are done."""
    try:
        return await reconcile_completed_checkpoint_artifacts(
            project=body.project,
            close=body.close,
            close_policy=body.close_policy,
            acted_by=body.acted_by,
            action_source=body.action_source,
            reason=body.reason,
            limit=body.limit,
        )
    except Exception as e:
        logger.error("Error reconciling completed checkpoint artifacts: %s", e)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reconcile completed checkpoint artifacts: {str(e)}",
        )

@router.post("/lifecycle-anomalies/completed-but-open", response_model=LifecycleAnomalyRepairResponse)
async def list_completed_but_open_lifecycle_anomalies(body: ArtifactLifecycleReconcileRequest):
    """Report completed-but-open lifecycle anomalies without closing artifacts."""
    if body.close:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Completed-but-open anomaly listing is read-only; use close_task/resolve_artifact after reviewing safe candidates.",
        )
    try:
        return await list_completed_but_open_anomalies(
            project=body.project,
            close_policy=body.close_policy,
            limit=body.limit,
        )
    except Exception as e:
        logger.error("Error listing completed-but-open lifecycle anomalies: %s", e)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list completed-but-open lifecycle anomalies: {str(e)}",
        )

@router.post("/completed-checkpoint-scope-review", response_model=ArtifactLifecycleScopeReviewResponse)
async def record_completed_checkpoint_scope_review(
    body: ArtifactLifecycleScopeReviewRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
):
    """Persist an operator review for a completed checkpoint's next_step scope."""
    content = build_checkpoint_scope_review_content(
        checkpoint_change_id=body.checkpoint_change_id,
        next_step_scope=body.next_step_scope,
        reason=body.reason,
    )
    try:
        change = await add_task_change(
            qdrant,
            ollama,
            task_id=body.task_id,
            body=ProjectTaskChangeCreate(
                project=body.project,
                change_type="note",
                content=content,
                why=body.reason,
                agent_id=body.acted_by,
                source=body.source,
                tags=["task_checkpoint_scope_review", f"next_step_scope:{body.next_step_scope}"],
            ),
        )
        return ArtifactLifecycleScopeReviewResponse(
            project=body.project,
            task_id=body.task_id,
            checkpoint_change_id=body.checkpoint_change_id,
            next_step_scope=body.next_step_scope,
            saved_change_id=str(change.id),
            content=content,
        )
    except Exception as e:
        logger.error("Error recording completed checkpoint scope review: %s", e)
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to record completed checkpoint scope review: {str(e)}",
        )


@router.post("/completed-checkpoint-scope-review/batch", response_model=ArtifactLifecycleScopeReviewBatchResponse)
async def record_completed_checkpoint_scope_review_batch(
    body: ArtifactLifecycleScopeReviewBatchRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
):
    """Persist multiple completed-checkpoint next_step scope reviews without closing artifacts."""
    result = ArtifactLifecycleScopeReviewBatchResponse(project=body.project)
    seen: set[tuple[str, str]] = set()
    for decision in body.decisions:
        key = (decision.task_id, decision.checkpoint_change_id)
        if key in seen:
            result.skipped.append(
                {
                    "task_id": decision.task_id,
                    "checkpoint_change_id": decision.checkpoint_change_id,
                    "reason": "duplicate_decision",
                }
            )
            continue
        seen.add(key)
        reason = decision.reason or body.default_reason
        content = build_checkpoint_scope_review_content(
            checkpoint_change_id=decision.checkpoint_change_id,
            next_step_scope=decision.next_step_scope,
            reason=reason,
        )
        try:
            change = await add_task_change(
                qdrant,
                ollama,
                task_id=decision.task_id,
                body=ProjectTaskChangeCreate(
                    project=body.project,
                    change_type="note",
                    content=content,
                    why=reason,
                    agent_id=body.acted_by,
                    source=body.source,
                    tags=["task_checkpoint_scope_review", f"next_step_scope:{decision.next_step_scope}"],
                ),
            )
            result.saved.append(
                ArtifactLifecycleScopeReviewResponse(
                    project=body.project,
                    task_id=decision.task_id,
                    checkpoint_change_id=decision.checkpoint_change_id,
                    next_step_scope=decision.next_step_scope,
                    saved_change_id=str(change.id),
                    content=content,
                )
            )
        except Exception as e:
            logger.error("Error recording completed checkpoint scope review batch item: %s", e)
            result.errors.append(
                {
                    "task_id": decision.task_id,
                    "checkpoint_change_id": decision.checkpoint_change_id,
                    "error": str(e),
                }
            )
    result.saved_count = len(result.saved)
    result.skipped_count = len(result.skipped)
    result.error_count = len(result.errors)
    return result


@router.get("/{artifact_key}", response_model=UnifiedArtifactRecord)
async def get_artifact(artifact_key: str):
    """Get a unified artifact by artifact_key.

    Args:
        artifact_key: Artifact key in format: {type}:{project}:{local_id}
            - Example: improvement:mnemoforge:2e8fdc03-fc0b-4f77-bbaa-99f570e8894c
            - Example: task:mnemoforge:6174ad7b-1fd9-4b6b-bb59-4f932b8cfc8c

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
