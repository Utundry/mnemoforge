import logging
import os
import time
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, status
from pydantic import BaseModel

from app.dependencies import OllamaDep, QdrantDep, ScorerDep
from app.models.memory import (
    ContextBundleResponse,
    ContextRequest,
    MemoryCreate,
    MemoryRecord,
    MemoryUpdate,
    SearchRequest,
    SearchResult,
)
from app.services.context_service import _context_svc
from app.services.embedding_gateway import embed_query, embed_text
from app.services.normalization_service import _norm_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memories", tags=["memories"])
hierarchy_router = APIRouter(tags=["knowledge-hierarchy"])


_DEDUP_THRESHOLD = float(__import__("os").getenv("SEMANTIC_DEDUP_THRESHOLD", "0.92"))


async def _semantic_dedup(memory_id, agent_id, vector, qdrant) -> None:
    """After insert: find near-duplicates and add bidirectional related_ids links."""
    try:
        from app.models.enums import MemoryType
        results = await qdrant.search(
            vector=vector,
            agent_id=agent_id or None,
            limit=5,
            overfetch_factor=1,
        )
        duplicates = [
            r for r, score in results
            if score >= _DEDUP_THRESHOLD and str(r.id) != str(memory_id)
        ]
        for dup in duplicates:
            await qdrant.add_link(memory_id, dup.id)
            await qdrant.add_link(dup.id, memory_id)
            logger.debug("Semantic dedup: linked %s ↔ %s (score≥%.2f)", memory_id, dup.id, _DEDUP_THRESHOLD)
    except Exception as exc:
        logger.debug("Semantic dedup failed (non-fatal): %s", exc)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MemoryRecord)
async def create_memory(body: MemoryCreate, qdrant: QdrantDep, ollama: OllamaDep, background_tasks: BackgroundTasks):
    from app.services.event_emitter import emit
    from app.services.learning_store import make_context_signature

    t0 = time.perf_counter()
    vector, embedding_meta = await embed_text(
        body.content,
        primary=ollama,
        purpose="memory_store",
        fallback_reason="memory_store_embedding_unavailable",
    )
    body.meta = {**(body.meta or {}), **embedding_meta}
    memory_id = await qdrant.insert(body, vector)
    duration_s = max(0.0, time.perf_counter() - t0)
    background_tasks.add_task(_semantic_dedup, memory_id, body.agent_id, vector, qdrant)

    ctx_sig_tool = make_context_signature(
        project=body.project or "unknown",
        task_type="tool",
        phase="call",
        category="memory_store",
        transport="api",
    )
    ctx_sig_mem = make_context_signature(
        project=body.project or "unknown",
        task_type="memory",
        phase="write",
        category=body.category or "unknown",
        transport="api",
    )

    background_tasks.add_task(emit, "tool_call",
        agent_id=body.agent_id or "",
        project=body.project or "",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "memory_store", "duration_s": duration_s})
    background_tasks.add_task(emit, "tool_result",
        agent_id=body.agent_id or "",
        project=body.project or "",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "memory_store", "success": True, "empty": False})

    background_tasks.add_task(emit, "memory_write",
        agent_id=body.agent_id or "",
        project=body.project or "",
        transport="api",
        context_signature=ctx_sig_mem,
        payload={"category": body.category, "memory_type": body.memory_type,
                 "source": body.source, "has_tags": bool(body.tags)})

    # Conditional reflex: if the agent keeps manually saving reports, record it as
    # a low-risk habit so automation can be suggested later.
    try:
        should_record = (
            body.category == "qa"
            or body.source == "implementation"
            or ("done" in (body.tags or []))
        )
        if should_record:
            from app.services.behavior_adaptation import record_behavior_event

            record_behavior_event(
                agent_id=body.agent_id,
                action_type="auto_save_result",
                accepted=True,
                context_signature=f"category:{body.category}",
            )
    except Exception as e:
        logger.debug("Behavior reflex record skipped: %s", e)

    if body.scope in {"domain", "principle", "meta"} and body.topic_path:
        try:
            from app.services.crystallization_service import _sync_canonical_to_tree

            await _sync_canonical_to_tree(body.topic_path, str(memory_id))
        except Exception as exc:
            logger.debug("Canonical tree sync skipped for %s: %s", memory_id, exc)

    return await qdrant.get(memory_id)


@router.get("/recent", response_model=list[MemoryRecord])
async def recent_memories(
    qdrant: QdrantDep,
    minutes: int = Query(10, ge=1, le=1440, description="Last N minutes"),
    agent_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """List memories added in the last N minutes, sorted by time descending."""
    return await qdrant.get_recent(minutes=minutes, agent_id=agent_id, limit=limit)


@router.get("/stats")
async def memory_stats(qdrant: QdrantDep):
    """Return total memory count and breakdown by memory_type."""
    stats = await qdrant.collection_stats()
    total = stats.get("points_count", 0)
    by_type: dict[str, int] = {}
    try:
        recent = await qdrant.get_recent(limit=500)
        for m in recent:
            mt = getattr(m, "memory_type", None) or (m.get("memory_type") if isinstance(m, dict) else "unknown") or "unknown"
            by_type[mt] = by_type.get(mt, 0) + 1
    except Exception:
        pass
    return {"total": total, "by_type": by_type}


@router.get("/{memory_id}", response_model=MemoryRecord)
async def get_memory(memory_id: UUID, qdrant: QdrantDep):
    record = await qdrant.get(memory_id)
    await qdrant.increment_access_count(memory_id)
    return record


@router.put("/{memory_id}", response_model=MemoryRecord)
async def update_memory(memory_id: UUID, body: MemoryUpdate, qdrant: QdrantDep, ollama: OllamaDep):
    new_vector = None
    if body.content is not None:
        new_vector, embedding_meta = await embed_text(
            body.content,
            primary=ollama,
            purpose="memory_update",
            fallback_reason="memory_update_embedding_unavailable",
        )
        body.meta = {**(body.meta or {}), **embedding_meta}
    return await qdrant.update(memory_id, body, new_vector)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: UUID, qdrant: QdrantDep):
    await qdrant.delete(memory_id)


# ── Dependency graph ────────────────────────────────────────────────────────────

class LinkRequest(BaseModel):
    target_id: UUID


@router.post("/{memory_id}/links", status_code=status.HTTP_204_NO_CONTENT)
async def add_link(memory_id: UUID, body: LinkRequest, qdrant: QdrantDep):
    """Add a directed link: memory_id → target_id (idempotent)."""
    await qdrant.add_link(memory_id, body.target_id)


@router.delete("/{memory_id}/links/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_link(memory_id: UUID, target_id: UUID, qdrant: QdrantDep):
    """Remove the link memory_id → target_id."""
    await qdrant.remove_link(memory_id, target_id)


@router.get("/{memory_id}/neighbors", response_model=list[MemoryRecord])
async def get_neighbors(memory_id: UUID, qdrant: QdrantDep):
    """Return all memories listed in related_ids of the given memory."""
    return await qdrant.get_neighbors(memory_id)


@router.post("/search", response_model=list[SearchResult])
async def search_memories(body: SearchRequest, qdrant: QdrantDep, ollama: OllamaDep, scorer: ScorerDep, background_tasks: BackgroundTasks):
    from app.services.learning_store import make_context_signature
    t0 = time.perf_counter()

    # Normalize query through agent glossary before embedding
    query = body.query
    if body.agent_id:
        try:
            norm = await _norm_svc.normalize(query, body.agent_id, qdrant)
            if norm.was_changed:
                logger.debug(
                    "Query normalized: %r → %r (applied: %s)",
                    query, norm.normalized, norm.applied,
                )
            query = norm.normalized
        except Exception as e:
            logger.warning("Normalization failed, using original query: %s", e)

    vector, _embedding_meta = await embed_query(query, primary=ollama, purpose="memory_search")
    raw = await qdrant.search(
        vector=vector,
        agent_id=body.agent_id,
        memory_type=body.memory_type,
        category=body.category,
        topic_prefix=body.topic_prefix,
        limit=body.limit,
        since_minutes=body.since_minutes,
    )
    from app.services.scoring_service import ContextHint
    ctx = ContextHint(
        project=body.context_project,
        file_path=body.context_file,
        task_type=body.context_task_type,
    )
    results = scorer.rank(raw, limit=body.limit, min_score=body.min_score, ctx=ctx)
    duration_s = max(0.0, time.perf_counter() - t0)
    from app.services.event_emitter import emit
    ctx_sig_tool = make_context_signature(
        project=body.context_project or "unknown",
        task_type="tool",
        phase="call",
        category="memory_search",
        transport="api",
    )
    ctx_sig_evt = make_context_signature(
        project=body.context_project or "unknown",
        task_type=body.context_task_type or "memory",
        phase="search",
        category=body.category or "unknown",
        transport="api",
    )
    background_tasks.add_task(emit, "tool_call",
        agent_id=body.agent_id or "",
        project=body.context_project or "",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "memory_search", "duration_s": duration_s})
    background_tasks.add_task(emit, "tool_result",
        agent_id=body.agent_id or "",
        project=body.context_project or "",
        transport="api",
        context_signature=ctx_sig_tool,
        payload={"tool_name": "memory_search", "success": True, "empty": (len(results) == 0)})
    background_tasks.add_task(emit, "memory_search",
        agent_id=body.agent_id or "",
        project=body.context_project or "",
        transport="api",
        context_signature=ctx_sig_evt,
        payload={"category": body.category, "memory_type": body.memory_type,
                 "results_count": len(results)})
    return results


_SCOPE_EXPAND_THRESHOLD = float(os.getenv("CONTEXT_SCOPE_EXPAND_THRESHOLD", "0.5"))
_CANONICAL_SCOPES = ["domain", "principle", "meta"]


@router.post("/context", response_model=ContextBundleResponse)
async def assemble_context(
    body: ContextRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    scorer: ScorerDep,
    background_tasks: BackgroundTasks,
):
    """
    Search memories and return a deduplicated, model-ready context bundle.

    Unlike /search which returns raw ranked results, /context assembles them into
    a single text block ready for direct injection into an LLM prompt:
    - Deduplicates near-identical memories (Jaccard similarity)
    - Groups by category with markdown headers
    - Respects max_tokens budget
    - Scope expansion: if top results are weak (avg score < threshold), appends
      domain/principle/meta canonical memories that match the same query.
    """
    # Normalize query through agent glossary before embedding
    from app.services.learning_store import make_context_signature
    t0 = time.perf_counter()
    query = body.query
    if body.agent_id:
        try:
            norm = await _norm_svc.normalize(query, body.agent_id, qdrant)
            query = norm.normalized
        except Exception as e:
            logger.warning("Normalization failed, using original query: %s", e)

    vector, _embedding_meta = await embed_query(query, primary=ollama, purpose="memory_context")
    raw = await qdrant.search(
        vector=vector,
        agent_id=body.agent_id,
        memory_type=body.memory_type,
        category=body.category,
        topic_prefix=body.topic_prefix,
        limit=body.limit,
        since_minutes=body.since_minutes,
    )
    from app.services.scoring_service import ContextHint
    ctx = ContextHint(
        project=getattr(body, "context_project", None),
        file_path=getattr(body, "context_file", None),
        task_type=getattr(body, "context_task_type", None),
    )
    ranked = scorer.rank(raw, limit=body.limit, min_score=body.min_score, ctx=ctx)

    # Scope expansion: if results are weak, supplement with canonical memories
    scope_expanded = False
    if not body.category:  # skip expansion when caller already filtered by category
        top_scores = [r.score for r in ranked[:3]]
        signal_weak = len(top_scores) == 0 or (sum(top_scores) / len(top_scores)) < _SCOPE_EXPAND_THRESHOLD
        if signal_weak:
            try:
                canonical_raw = await qdrant.search(
                    vector=vector,
                    limit=5,
                    scope_filter=_CANONICAL_SCOPES,
                    topic_prefix=body.topic_prefix,
                )
                if canonical_raw:
                    existing_ids = {r.memory.id for r in ranked}
                    novel = [(rec, sim) for rec, sim in canonical_raw if rec.id not in existing_ids]
                    if novel:
                        canonical_ranked = scorer.rank(novel, limit=5, min_score=0.0)
                        ranked = ranked + canonical_ranked
                        scope_expanded = True
            except Exception as _e:
                logger.debug("scope expansion failed (non-fatal): %s", _e)

    bundle = _context_svc.assemble(ranked, max_tokens=body.max_tokens, fmt=body.format)
    duration_s = max(0.0, time.perf_counter() - t0)
    ctx_sig_tool = make_context_signature(
        project=body.context_project or "unknown",
        task_type="tool",
        phase="call",
        category="memory_context",
        transport="api",
    )

    # USE event: mark retrieved memories as used (updates last_access_ts + project activity gate)
    used_ids = [r.memory.id for r in ranked[:bundle.used_count]]
    if used_ids:
        try:
            await qdrant.mark_used(used_ids, project=body.context_project)
        except Exception as _e:
            logger.debug("mark_used failed (non-fatal): %s", _e)

    # Session tracking: link this /context call to a session_id (episode_id) for later outcome feedback.
    session_id = body.session_id
    if not session_id:
        try:
            import uuid as _uuid
            session_id = str(_uuid.uuid4())
        except Exception:
            session_id = None

    if session_id:
        try:
            from app.services.event_emitter import emit
            # Canonical tool-call signal for GLM Mirror (matches trigger_dsl whitelists)
            background_tasks.add_task(
                emit,
                "tool_call",
                agent_id=body.agent_id or "",
                project=body.context_project or "",
                transport="api",
                episode_id=session_id,
                context_signature=ctx_sig_tool,
                payload={"tool_name": "memory_context", "duration_s": duration_s},
            )
            background_tasks.add_task(
                emit,
                "tool_result",
                agent_id=body.agent_id or "",
                project=body.context_project or "",
                transport="api",
                episode_id=session_id,
                context_signature=ctx_sig_tool,
                payload={"tool_name": "memory_context", "success": True, "empty": (bundle.used_count == 0)},
            )

            # Session tracking: always record a memory_use event (even if empty) so UI can show the session_id
            # and outcomes can later link to the episode_id.
            background_tasks.add_task(
                emit,
                "memory_use",
                agent_id=body.agent_id or "",
                project=body.context_project or "",
                transport="api",
                episode_id=session_id,
                context_signature="memories/context",
                payload={
                    "used_ids": [str(x) for x in used_ids],
                    "used_count": bundle.used_count,
                    "source_count": bundle.source_count,
                    "query_len": len(body.query or ""),
                    "scope_expanded": scope_expanded,
                },
            )
        except Exception as _e:
            logger.debug("memory_use emit skipped (non-fatal): %s", _e)

    # Scout observer: collect pending hints + trigger background fetch if signal is weak
    from app.models.memory import PendingHint
    from app.services.learning_store import get_learning_store as _get_ls
    pending_hints: list[PendingHint] = []
    try:
        _ls = _get_ls()
        _raw_hints = await _ls.get_pending_scout_hints(project=body.context_project or "", limit=3)
        for _h in _raw_hints:
            _meta = _h.get("meta") or {}
            _title = _meta.get("title") or _h.get("content", "")[:60]
            _domain = (_h.get("tags") or [""])[2] if len(_h.get("tags") or []) > 2 else ""
            pending_hints.append(PendingHint(id=_h["id"], title=_title, domain=_domain))

        if signal_weak and not pending_hints and body.context_project:
            async def _bg_scout():
                try:
                    from app.services.best_practice_scout import check_sufficiency, fetch_best_practices
                    from app.routers.learning import scout_fetch
                    suf = await check_sufficiency(
                        _ls,
                        project=body.context_project,
                        task=body.query or "",
                        agent_id=body.agent_id or "",
                    )
                    if not suf.sufficient:
                        from app.routers.learning import ScoutFetchRequest
                        from fastapi import BackgroundTasks as _BT
                        await scout_fetch(
                            ScoutFetchRequest(
                                project=body.context_project,
                                task=body.query or "",
                                domains=suf.missing_domains or [],
                                agent_id=body.agent_id or "scout",
                            ),
                            _BT(),
                        )
                except Exception as _e:
                    logger.debug("background scout fetch failed (non-fatal): %s", _e)
            background_tasks.add_task(_bg_scout)
    except Exception as _e:
        logger.debug("pending_hints fetch failed (non-fatal): %s", _e)

    return ContextBundleResponse(
        context=bundle.context,
        source_count=bundle.source_count,
        used_count=bundle.used_count,
        deduplicated_count=bundle.deduplicated_count,
        categories=bundle.categories,
        tokens_estimate=bundle.tokens_estimate,
        scope_expanded=scope_expanded,
        session_id=session_id,
        pending_hints=pending_hints,
    )


# ── Knowledge tree ─────────────────────────────────────────────────────────────

class TreeSliceRequest(BaseModel):
    """Return all memories whose topic_path starts with the given prefix, grouped by scope."""
    topic_path: str
    scopes: Optional[list[str]] = None   # filter by scope(s); None = all scopes
    limit: int = 50


class TreeSliceResponse(BaseModel):
    topic_path: str
    total: int
    by_scope: dict[str, list[MemoryRecord]]


@router.post("/tree-slice", response_model=TreeSliceResponse)
async def tree_slice(body: TreeSliceRequest, qdrant: QdrantDep):
    """
    Return all memories under a topic_path prefix, grouped by scope level.

    Example: topic_path="python/fastapi" returns all memories whose topic_path
    is "python/fastapi" or starts with "python/fastapi/".

    Use scopes=["domain","principle","meta"] to fetch only canonicals,
    or scopes=["config","project","family"] for leaf memories only.
    """
    records = await qdrant.scroll_by_topic_path(
        topic_prefix=body.topic_path,
        scopes=body.scopes,
        limit=body.limit,
    )

    by_scope: dict[str, list[MemoryRecord]] = {}
    for rec in records:
        by_scope.setdefault(rec.scope, []).append(rec)

    return TreeSliceResponse(
        topic_path=body.topic_path,
        total=len(records),
        by_scope=by_scope,
    )


class CanonicalItemResponse(BaseModel):
    id: str
    topic_path: str
    scope: str
    content: str
    supports: list[str]
    support_count: int
    confidence: float
    suppressed: bool = False
    canonical_status: str = "active"
    merged_into: Optional[str] = None
    candidate_revision: Optional[dict] = None
    last_review_action: Optional[str] = None
    last_reviewed_by: Optional[str] = None
    last_review_source: Optional[str] = None
    last_reviewed_at: Optional[str] = None
    last_review_reason: Optional[str] = None
    project: Optional[str] = None
    timestamp: str


class CanonicalsByScopeResponse(BaseModel):
    scope: str
    total: int
    items: list[CanonicalItemResponse]


class KnowledgeHierarchyResponse(BaseModel):
    topic_prefix: Optional[str] = None
    scope_order: list[str]
    totals: dict[str, int]
    by_scope: dict[str, list[CanonicalItemResponse]]
    lifecycle: dict[str, int]


class CanonicalStatusUpdateRequest(BaseModel):
    suppressed: bool
    reason: Optional[str] = None
    reviewed_by: str = "user"
    review_source: str = "inline_user_approval"


class CanonicalStatusResponse(BaseModel):
    id: str
    suppressed: bool
    canonical_status: str
    reason: Optional[str] = None
    last_review_action: Optional[str] = None
    last_reviewed_by: Optional[str] = None
    last_review_source: Optional[str] = None
    last_reviewed_at: Optional[str] = None
    last_review_reason: Optional[str] = None


class CanonicalMergeRequest(BaseModel):
    target_id: str
    reviewed_by: str = "user"
    review_source: str = "inline_user_approval"
    reason: Optional[str] = None


class CanonicalMergeResponse(BaseModel):
    source_id: str
    target_id: str
    merged_support_count: int
    topic_path: str
    last_review_action: Optional[str] = None
    last_reviewed_by: Optional[str] = None
    last_review_source: Optional[str] = None
    last_reviewed_at: Optional[str] = None
    last_review_reason: Optional[str] = None


class CanonicalCandidateActionResponse(BaseModel):
    id: str
    topic_path: str
    scope: str
    content: str
    supports: list[str]
    support_count: int
    confidence: float
    suppressed: bool = False
    canonical_status: str = "active"
    merged_into: Optional[str] = None
    candidate_revision: Optional[dict] = None
    last_review_action: Optional[str] = None
    last_reviewed_by: Optional[str] = None
    last_review_source: Optional[str] = None
    last_reviewed_at: Optional[str] = None
    last_review_reason: Optional[str] = None
    project: Optional[str] = None
    timestamp: str


class CanonicalCandidateReviewRequest(BaseModel):
    reviewed_by: str = "user"
    review_source: str = "inline_user_approval"
    reason: Optional[str] = None


@hierarchy_router.get("/canonicals/by-scope", response_model=CanonicalsByScopeResponse)
async def canonicals_by_scope(
    qdrant: QdrantDep,
    scope: str = Query(..., pattern="^(domain|principle|meta)$"),
    topic_prefix: Optional[str] = Query(None),
    include_suppressed: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
):
    from app.config import settings
    from app.services.crystallization_service import list_canonicals

    items = await list_canonicals(
        qdrant._client,
        settings.qdrant_collection_name,
        scopes=[scope],
        topic_prefix=topic_prefix,
        include_suppressed=include_suppressed,
        limit=limit,
    )
    return CanonicalsByScopeResponse(scope=scope, total=len(items), items=items)


@hierarchy_router.get("/knowledge-hierarchy", response_model=KnowledgeHierarchyResponse)
async def knowledge_hierarchy(
    qdrant: QdrantDep,
    topic_prefix: Optional[str] = Query(None),
    include_suppressed: bool = Query(False),
    limit_per_scope: int = Query(25, ge=1, le=200),
    reconcile: bool = Query(False),
):
    from app.config import settings
    from app.services.crystallization_service import get_knowledge_hierarchy

    data = await get_knowledge_hierarchy(
        qdrant._client,
        settings.qdrant_collection_name,
        topic_prefix=topic_prefix,
        include_suppressed=include_suppressed,
        limit_per_scope=limit_per_scope,
        reconcile=reconcile,
    )
    return KnowledgeHierarchyResponse(**data)


@hierarchy_router.patch("/canonicals/{canonical_id}/status", response_model=CanonicalStatusResponse)
async def update_canonical_status(
    canonical_id: str,
    body: CanonicalStatusUpdateRequest,
    qdrant: QdrantDep,
):
    from app.config import settings
    from app.services.crystallization_service import set_canonical_status

    try:
        data = await set_canonical_status(
            qdrant._client,
            settings.qdrant_collection_name,
            canonical_id=canonical_id,
            suppressed=body.suppressed,
            reason=body.reason,
            reviewed_by=body.reviewed_by,
            review_source=body.review_source,
        )
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CanonicalStatusResponse(**data)


@hierarchy_router.post("/canonicals/{canonical_id}/merge", response_model=CanonicalMergeResponse)
async def merge_canonical_endpoint(
    canonical_id: str,
    body: CanonicalMergeRequest,
    qdrant: QdrantDep,
):
    from app.config import settings
    from app.services.crystallization_service import merge_canonicals

    try:
        data = await merge_canonicals(
            qdrant._client,
            settings.qdrant_collection_name,
            source_id=canonical_id,
            target_id=body.target_id,
            reviewed_by=body.reviewed_by,
            review_source=body.review_source,
            reason=body.reason,
        )
    except ValueError as exc:
        from fastapi import HTTPException

        status_code = 400 if "same" in str(exc).lower() or "scope" in str(exc).lower() else 404
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return CanonicalMergeResponse(**data)


@hierarchy_router.post("/canonicals/{canonical_id}/apply-candidate", response_model=CanonicalCandidateActionResponse)
async def apply_canonical_candidate_endpoint(
    canonical_id: str,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    body: CanonicalCandidateReviewRequest | None = None,
):
    from app.config import settings
    from app.services.crystallization_service import apply_canonical_candidate

    review = body or CanonicalCandidateReviewRequest()
    try:
        data = await apply_canonical_candidate(
            qdrant._client,
            settings.qdrant_collection_name,
            canonical_id=canonical_id,
            ollama_svc=ollama,
            reviewed_by=review.reviewed_by,
            review_source=review.review_source,
            reason=review.reason,
        )
    except ValueError as exc:
        from fastapi import HTTPException

        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return CanonicalCandidateActionResponse(**data)


@hierarchy_router.post("/canonicals/{canonical_id}/discard-candidate", response_model=CanonicalCandidateActionResponse)
async def discard_canonical_candidate_endpoint(
    canonical_id: str,
    qdrant: QdrantDep,
    body: CanonicalCandidateReviewRequest | None = None,
):
    from app.config import settings
    from app.services.crystallization_service import discard_canonical_candidate

    review = body or CanonicalCandidateReviewRequest()
    try:
        data = await discard_canonical_candidate(
            qdrant._client,
            settings.qdrant_collection_name,
            canonical_id=canonical_id,
            reviewed_by=review.reviewed_by,
            review_source=review.review_source,
            reason=review.reason,
        )
    except ValueError as exc:
        from fastapi import HTTPException

        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return CanonicalCandidateActionResponse(**data)
