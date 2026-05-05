from datetime import datetime, timezone
import logging
import time
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
from app.config import settings
from app.models.memory import ImprovementCreate, ImprovementRecord, ImprovementReviewRequest
from app.services.project_identity_service import resolve_project_id

router = APIRouter(prefix="/improvements", tags=["improvements"])


class ImprovementStatusActionRequest(BaseModel):
    acted_by: str = Field("user", min_length=1, max_length=256)
    action_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)


def _row_to_record(row: dict) -> ImprovementRecord:
    created_ts = row.get("created_at") or 0.0
    resolved_ts = row.get("resolved_at")
    return ImprovementRecord(
        id=UUID(row["id"]),
        title=row["title"],
        description=row["description"],
        project=row["project"],
        agent_id=row["agent_id"],
        importance_score=row["importance_score"],
        timestamp=datetime.fromtimestamp(created_ts, tz=timezone.utc),
        status=row["status"],
        tags=row.get("tags") or [],
        stage=row.get("stage") or "proposal",
        verdict=row.get("verdict") or None,
        resolved_at=(
            datetime.fromtimestamp(resolved_ts, tz=timezone.utc) if resolved_ts else None
        ),
        report_count=row.get("report_count") or 1,
        report_history=row.get("report_history") or [],
        last_status_action=row.get("last_status_action"),
        last_status_acted_by=row.get("last_status_acted_by"),
        last_status_action_source=row.get("last_status_action_source"),
        last_status_action_at=(
            datetime.fromtimestamp(row["last_status_action_at"], tz=timezone.utc)
            if row.get("last_status_action_at")
            else None
        ),
        last_status_action_reason=row.get("last_status_action_reason"),
        last_quality_review_by=row.get("last_quality_review_by"),
        last_quality_review_source=row.get("last_quality_review_source"),
        last_quality_review_at=(
            datetime.fromtimestamp(row["last_quality_review_at"], tz=timezone.utc)
            if row.get("last_quality_review_at")
            else None
        ),
        last_quality_review_reason=row.get("last_quality_review_reason"),
    )


async def _generate_report(items: list[ImprovementRecord], project: str) -> dict:
    """Aggregate stats and use GLM to generate a narrative report."""
    from app.services.cloud_llm import cloud_available
    from app.services.llm_gateway import get_cloud_gateway

    total = len(items)
    resolved = [i for i in items if i.status == "resolved"]
    open_ = [i for i in items if i.status == "open"]

    tag_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    for item in items:
        for tag in (item.tags or []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        stage_counts[item.stage or "proposal"] = stage_counts.get(item.stage or "proposal", 0) + 1
        if item.verdict:
            verdict_counts[item.verdict] = verdict_counts.get(item.verdict, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:8]

    top_open = sorted(open_, key=lambda x: -(x.importance_score or 0))[:5]
    top_resolved = sorted(resolved, key=lambda x: -(x.importance_score or 0))[:5]

    stats = {
        "project": project,
        "total": total,
        "resolved": len(resolved),
        "open": len(open_),
        "resolved_pct": round(len(resolved) / total * 100, 1) if total else 0,
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "by_stage": dict(sorted(stage_counts.items(), key=lambda x: (-x[1], x[0]))),
        "by_verdict": dict(sorted(verdict_counts.items(), key=lambda x: (-x[1], x[0]))),
        "top_open": [{"title": i.title, "importance": i.importance_score, "id": str(i.id)} for i in top_open],
        "top_resolved": [{"title": i.title, "importance": i.importance_score} for i in top_resolved],
    }

    narrative = None
    if cloud_available() and total > 0:
        open_list = "\n".join(f"- [{i.importance_score:.2f}] {i.title}" for i in top_open) or "none"
        resolved_list = "\n".join(f"- [{i.importance_score:.2f}] {i.title}" for i in top_resolved[:3]) or "none"
        prompt = f"""You are a technical writer. Generate a concise project status report in markdown.

Project: {project}
Total improvements: {total} ({len(resolved)} resolved / {len(open_)} open, {stats['resolved_pct']}%)
Top tags: {', '.join(t for t, _ in top_tags)}
By stage: {', '.join(f'{k}={v}' for k, v in sorted(stage_counts.items())) or 'none'}
By verdict: {', '.join(f'{k}={v}' for k, v in sorted(verdict_counts.items())) or 'none'}

Open (by importance):
{open_list}

Recently resolved (by importance):
{resolved_list}

Write: 1) one-paragraph executive summary, 2) Key achievements (bullets), 3) Priorities (bullets).
Be concise, no fluff. Use markdown."""
        try:
            narrative = await get_cloud_gateway().generate(
                prompt,
                system="You write compact technical status reports with no fluff.",
                task_type="text_summarization",
                mode="economy",
                max_tokens=450,
                temperature=0.2,
                allow_local_fallback=True,
                prefer_local=True,
            )
        except Exception:
            narrative = None

    return {"stats": stats, "narrative": narrative}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ImprovementRecord)
async def create_improvement(body: ImprovementCreate, background_tasks: BackgroundTasks, qdrant: QdrantDep, ollama: OllamaDep):
    """Report a missing feature or incorrect behavior encountered by an LLM or user."""
    from app.services.improvements_store import get_improvements_store
    from app.services.event_emitter import emit
    from app.services.learning_store import make_context_signature

    t0 = time.perf_counter()
    store = get_improvements_store()
    uid, created = await store.upsert_by_title(
        title=body.title,
        description=body.description,
        project=body.project,
        agent_id=body.agent_id,
        importance_score=body.importance_score,
        tags=body.tags,
        stage=body.stage,
        verdict=body.verdict,
    )
    row = await store.get(uid)
    if not row:
        raise HTTPException(status_code=500, detail="Failed to retrieve improvement")

    # Return 200 for duplicates so callers know it was merged, not created fresh
    if not created:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content=_row_to_record(row).model_dump(mode="json"),
        )

    duration_s = max(0.0, time.perf_counter() - t0)
    ctx_sig_tool = make_context_signature(
        project=body.project or "unknown",
        task_type="tool",
        phase="call",
        category="report_issue",
        transport="api",
    )
    ctx_sig_evt = make_context_signature(
        project=body.project or "unknown",
        task_type="improvements",
        phase="create",
        category="improvement",
        transport="api",
    )

    background_tasks.add_task(
        emit,
        "tool_call",
        agent_id=body.agent_id or "",
        project=body.project or "",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "report_issue", "duration_s": duration_s},
    )
    background_tasks.add_task(
        emit,
        "tool_result",
        agent_id=body.agent_id or "",
        project=body.project or "",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "report_issue", "success": True, "empty": False},
    )
    background_tasks.add_task(emit, "improvement_created",
        agent_id=body.agent_id or "",
        project=body.project or "",
        transport="api",
        context_signature=ctx_sig_evt,
        payload={"importance_score": body.importance_score, "tags": body.tags or []})

    # Auto-create tree node for this improvement
    background_tasks.add_task(_sync_improvement_to_tree, uid, body.project, body.title, body.description, body.tags or [])
    background_tasks.add_task(_bootstrap_task_for_improvement, uid)

    return _row_to_record(row)


async def _bootstrap_task_for_improvement(improvement_id) -> None:
    try:
        from app.dependencies import get_ollama, get_qdrant
        from app.services.improvements_store import get_improvements_store
        from app.services.project_task_service import ensure_task_for_improvement, record_improvement_task_change

        store = get_improvements_store()
        row = await store.get(improvement_id)
        if not row:
            return
        qdrant = get_qdrant()
        ollama = get_ollama()
        await ensure_task_for_improvement(qdrant, ollama, row)
        await record_improvement_task_change(
            qdrant,
            ollama,
            improvement_row=row,
            change_type="task_created",
            content=f"Task bootstrapped from improvement '{row['title']}'.",
            why="Ensure a canonical task entity exists for memoirs and future task changes.",
            source="improvement_created",
        )
    except Exception as e:
        logger.warning("Failed to bootstrap task for improvement: %s", e)


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


async def _sync_improvement_to_tree(improvement_id, project: str, title: str, description: str, tags: list[str]) -> None:
    """Find or create a task node in the project tree for this improvement."""
    try:
        from app.services.project_tree_store import get_tree_store
        from app.services.improvements_store import get_improvements_store
        ts = get_tree_store()
        imp_store = get_improvements_store()

        # Find the project root node by topic_path or title
        projects = ts.get_projects()
        parent = next(
            (p for p in projects if p.get("topic_path", "").split("/")[0] == project or p.get("title", "").lower() == project.lower()),
            None,
        )
        parent_id = parent["id"] if parent else None

        improvement_row = await imp_store.get(improvement_id)
        node = None
        linked_node_id = str((improvement_row or {}).get("node_id") or "").strip()
        if linked_node_id:
            node = ts.get_node(linked_node_id)
        if node is None:
            node = ts.find_node_by_improvement_id(str(improvement_id))
        if node is None:
            node = ts.find_equivalent_node(
                title=title,
                type="task",
                parent_id=parent_id,
                status="planning",
            )

        if node is None:
            node_id = ts.create_node(
                title=title,
                type="task",
                parent_id=parent_id,
                description=description,
                status="planning",
                tags=tags,
            )
            node = ts.get_node(node_id)
        else:
            node_id = str(node["id"])

        node = node or ts.get_node(node_id)
        if node is None:
            return

        merged_tags = list(dict.fromkeys(list(node.get("tags") or []) + list(tags or [])))
        merged_description = description or str(node.get("description") or "")
        meta = node.get("meta_json") or {}
        meta["improvement_id"] = str(improvement_id)
        ts.update_node(
            node_id,
            parent_id=parent_id,
            description=merged_description,
            tags=merged_tags,
            meta_json=meta,
        )
        imp_store.set_node_id(improvement_id, node_id)
        logger.info("Improvement %s → tree node %s", improvement_id, node_id)
    except Exception as e:
        logger.warning("Failed to sync improvement to tree: %s", e)


@router.get("", response_model=list[ImprovementRecord])
async def list_improvements(
    project: Optional[str] = Query(None),
    status: Optional[str] = Query("open", description="open | resolved | all"),
    limit: int = Query(50, ge=1, le=200),
):
    """List improvements, optionally filtered by project and status."""
    from app.services.improvements_store import get_improvements_store

    store = get_improvements_store()
    canonical_project = resolve_project_id(project) if project else None
    status_filter = None if status == "all" else status
    rows = await store.list(project=canonical_project, status=status_filter, limit=limit)
    return [_row_to_record(r) for r in rows]


@router.get("/report")
async def improvements_report(
    project: Optional[str] = Query(None),
):
    """Return aggregated stats + GLM-generated narrative for all improvements."""
    from app.services.improvements_store import get_improvements_store
    store = get_improvements_store()
    canonical_project = resolve_project_id(project) if project else None
    rows = await store.list(project=canonical_project, status=None, limit=500)
    items = [_row_to_record(r) for r in rows]
    return await _generate_report(items, canonical_project or "all")


@router.get("/report/translate")
async def improvements_report_translate(
    project: str | None = Query(default=None),
) -> dict:
    """Translate the narrative part of the improvements report on demand."""
    from app.services.project_tree_doc import translate_doc
    from app.services.improvements_store import get_improvements_store

    store = get_improvements_store()
    canonical_project = resolve_project_id(project) if project else None
    rows = await store.list(project=canonical_project, status=None, limit=1000)
    items = [_row_to_record(r) for r in rows]
    report = await _generate_report(items, canonical_project or "all")
    narrative = (report.get("narrative") or "").strip()
    if not narrative:
        raise HTTPException(status_code=404, detail="No narrative available to translate")

    try:
        translated = await translate_doc(narrative, settings.glm_response_language)
    except RuntimeError as exc:
        detail = str(exc).strip() or "Translation failed"
        status_code = 503 if "no cloud llm configured" in detail.lower() else 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {
        "project": canonical_project or "all",
        "language": settings.glm_response_language,
        "original": narrative,
        "translated": translated,
        "stats": report.get("stats") or {},
    }


@router.get("/{improvement_id}", response_model=ImprovementRecord)
async def get_improvement(improvement_id: UUID):
    """Fetch a single improvement by ID."""
    from app.services.improvements_store import get_improvements_store

    store = get_improvements_store()
    row = await store.get(improvement_id)
    if not row:
        raise HTTPException(status_code=404, detail="Improvement not found")
    return _row_to_record(row)


@router.post("/{improvement_id}/memoir", response_model=dict)
async def generate_memoir(improvement_id: UUID, qdrant: QdrantDep, ollama: OllamaDep):
    """Manually generate (or regenerate) a memoir for an improvement task."""
    from app.services.memoir_service import generate_and_store_memoir
    memoir_id = await generate_and_store_memoir(
        task_id=str(improvement_id),
        qdrant_client=qdrant._client,
        collection=settings.qdrant_collection_name,
        ollama=ollama,
    )
    if not memoir_id:
        raise HTTPException(status_code=500, detail="Memoir generation failed")
    return {"improvement_id": str(improvement_id), "memoir_id": memoir_id}


@router.patch("/{improvement_id}/resolve", response_model=dict)
async def resolve_improvement(
    improvement_id: UUID,
    body: ImprovementStatusActionRequest,
    queue: JobQueueDep,
    background_tasks: BackgroundTasks,
    qdrant: QdrantDep,
    ollama: OllamaDep,
):
    """Mark an improvement as resolved while preserving the legacy endpoint."""
    from app.models.unified_artifact import UnifiedArtifactResolveRequest
    from app.services.docs_service import invalidate_docs_cache
    from app.services.improvements_store import get_improvements_store
    from app.services.project_task_service import record_improvement_task_change
    from app.services.unified_artifact_service import get_unified_artifact_service

    store = get_improvements_store()
    row = await store.get(improvement_id)
    if not row:
        raise HTTPException(status_code=404, detail="Improvement not found")

    project = row["project"]
    artifact_key = f"improvement:{project}:{improvement_id}"
    request = UnifiedArtifactResolveRequest(
        acted_by=body.acted_by,
        action_source=body.action_source,
        reason=body.reason,
    )
    await get_unified_artifact_service().resolve_artifact(artifact_key, request)
    updated = await store.get(improvement_id)
    if updated:
        await record_improvement_task_change(
            qdrant,
            ollama,
            improvement_row=updated,
            change_type="status_change",
            content="Improvement marked resolved.",
            why=body.reason or "Resolution was explicitly recorded.",
            source=body.action_source or "inline_user_approval",
        )

    await queue.submit(
        "task_memoir",
        {
            "task_id": str(improvement_id),
            "project": project,
        },
    )
    invalidate_docs_cache(project)
    await queue.submit("docs_rebuild", {"project": project})

    node_id = row.get("node_id", "")
    if node_id:
        background_tasks.add_task(_sync_resolved_to_tree_node, node_id)

    return {"id": str(improvement_id), "status": "resolved"}


@router.patch("/{improvement_id}/review", response_model=ImprovementRecord)
async def review_improvement(improvement_id: UUID, body: ImprovementReviewRequest):
    """Set stage/verdict for an improvement without changing lifecycle status."""
    from app.services.improvements_store import get_improvements_store

    if body.stage is None and body.verdict is None:
        raise HTTPException(status_code=400, detail="At least one of stage or verdict is required")

    store = get_improvements_store()
    project = await store.review(
        improvement_id,
        stage=body.stage,
        verdict=body.verdict,
        reviewed_by=body.reviewed_by,
        review_source=body.review_source,
        reason=body.reason,
    )
    if not project:
        raise HTTPException(status_code=404, detail="Improvement not found")
    row = await store.get(improvement_id)
    if not row:
        raise HTTPException(status_code=404, detail="Improvement not found")
    return _row_to_record(row)
