from datetime import datetime, timezone
import logging
import time
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status

logger = logging.getLogger(__name__)

from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
from app.config import settings
from app.models.memory import ImprovementCreate, ImprovementRecord

router = APIRouter(prefix="/improvements", tags=["improvements"])


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
        resolved_at=(
            datetime.fromtimestamp(resolved_ts, tz=timezone.utc) if resolved_ts else None
        ),
        report_count=row.get("report_count") or 1,
        report_history=row.get("report_history") or [],
    )


async def _generate_report(items: list[ImprovementRecord], project: str) -> dict:
    """Aggregate stats and use GLM to generate a narrative report."""
    from app.services.cloud_llm import cloud_available, cloud_complete

    total = len(items)
    resolved = [i for i in items if i.status == "resolved"]
    open_ = [i for i in items if i.status == "open"]

    tag_counts: dict[str, int] = {}
    for item in items:
        for tag in (item.tags or []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
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

Open (by importance):
{open_list}

Recently resolved (by importance):
{resolved_list}

Write: 1) one-paragraph executive summary, 2) Key achievements (bullets), 3) Priorities (bullets).
Be concise, no fluff. Use markdown."""
        try:
            narrative = await cloud_complete(prompt, max_tokens=600, temperature=0.3)
        except Exception:
            narrative = None

    return {"stats": stats, "narrative": narrative}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ImprovementRecord)
async def create_improvement(body: ImprovementCreate, background_tasks: BackgroundTasks):
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

    return _row_to_record(row)


async def _sync_resolved_to_tree_node(node_id: str) -> None:
    """Mark the linked tree node as done when improvement is resolved."""
    try:
        from app.services.project_tree_store import get_tree_store
        ts = get_tree_store()
        node = ts.get_node(node_id)
        if node and node.get("status") != "done":
            ts.update_node(node_id, status="done")
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

        # Create task node linked to this improvement
        node_id = ts.create_node(
            title=title,
            type="task",
            parent_id=parent_id,
            description=description,
            status="planning",
            tags=tags,
        )
        # Store back-reference in both directions
        imp_store.set_node_id(improvement_id, node_id)
        node = ts.get_node(node_id)
        meta = node.get("meta_json") or {}
        meta["improvement_id"] = str(improvement_id)
        ts.update_node(node_id, meta_json=meta)
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
    status_filter = None if status == "all" else status
    rows = await store.list(project=project, status=status_filter, limit=limit)
    return [_row_to_record(r) for r in rows]


@router.get("/report")
async def improvements_report(
    project: Optional[str] = Query(None),
):
    """Return aggregated stats + GLM-generated narrative for all improvements."""
    from app.services.improvements_store import get_improvements_store
    store = get_improvements_store()
    rows = await store.list(project=project, status=None, limit=500)
    items = [_row_to_record(r) for r in rows]
    return await _generate_report(items, project or "all")


@router.get("/report/translate")
async def improvements_report_translate(
    project: str | None = Query(default=None),
) -> dict:
    """Translate the narrative part of the improvements report on demand."""
    from app.services.project_tree_doc import translate_doc
    from app.services.improvements_store import get_improvements_store

    store = get_improvements_store()
    rows = await store.list(project=project, status=None, limit=1000)
    items = [_row_to_record(r) for r in rows]
    report = await _generate_report(items, project or "all")
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
        "project": project or "all",
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


@router.patch("/{improvement_id}/resolve", response_model=dict)
async def resolve_improvement(improvement_id: UUID, queue: JobQueueDep, background_tasks: BackgroundTasks):
    """Mark an improvement as resolved. Auto-generates a task memoir in the background."""
    from app.services.improvements_store import get_improvements_store
    store = get_improvements_store()
    row = await store.get(improvement_id)
    if not row:
        raise HTTPException(status_code=404, detail="Improvement not found")
    project = await store.resolve(improvement_id)
    await queue.submit("task_memoir", {
        "task_id": str(improvement_id),
        "project": project,
    })
    from app.services.docs_service import invalidate_docs_cache
    invalidate_docs_cache(project)
    await queue.submit("docs_rebuild", {"project": project})

    # improvement→resolved: auto-mark linked tree node as done
    node_id = row.get("node_id", "")
    if node_id:
        background_tasks.add_task(_sync_resolved_to_tree_node, node_id)

    return {"id": str(improvement_id), "status": "resolved"}


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
