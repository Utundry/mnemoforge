"""
Learning Ledger API router.

Endpoints:
  POST /learning/events                    — record a canonical learning event
  GET  /learning/events                    — list recent events (with filters)
  GET  /learning/artifacts                 — list artifacts (with filters)
  POST /learning/artifacts/{id}/rate       — rate an artifact (useful / not_useful)
  POST /learning/artifacts/{id}/promote    — advance artifact scope one step
  GET  /learning/report                    — top 2-3 candidates for human review
  POST /learning/candidates                — user-initiated improvement (create candidate)
  POST /learning/candidates/{id}/approve   — approve candidate → runtime_hint
  POST /learning/candidates/{id}/reject    — reject candidate → archived
  POST /learning/candidates/{id}/defer     — defer candidate (raise threshold, delay resurface)
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.services.learning_store import (
    get_learning_store,
    make_context_signature,
    make_artifact_key,
    min_evidence_for,
)
from app.services.text_localization import (
    hydrate_artifact_display_fields,
    prepare_artifact_texts,
    translate_artifact_fields,
)
from app.services.trigger_dsl import (
    ALLOWED_ACTION_TYPES,
    validate_if_then_rule,
    validate_trigger,
)

router = APIRouter(prefix="/learning", tags=["learning"])

CANONICAL_EVENT_TYPES: frozenset[str] = frozenset({
    "episode_start",
    "episode_end",
    "user_request",
    "user_feedback",
    "dialogue_excerpt",
    "dialogue_signal",
    "tool_call",
    "tool_result",
    "memory_use",
    "outcome_recorded",
    "session_outcome",   # alias used by outcome-recorder hook
    "memory_write",
    "artifact_promoted",
    "artifact_suggested",
    "artifact_feedback",
    "llm_mirror",
    "external_best_practice_requested",
    "candidate_approved",
    "candidate_rejected",
    "improvement_created",
})

# ── Request / Response models ──────────────────────────────────────────────────

class EventCreate(BaseModel):
    event_type: str = Field(..., description=f"One of: {sorted(CANONICAL_EVENT_TYPES)}")
    agent_id: str = Field("", max_length=256)
    project: str = Field("", max_length=256)
    transport: str = Field("mcp", max_length=64)
    episode_id: str = Field("", max_length=256)
    context_signature: str = Field("", max_length=512)
    payload: dict = Field(default_factory=dict)


class ContextSignatureRequest(BaseModel):
    project: str = Field("unknown", max_length=256)
    task_type: str = Field("unknown", max_length=128)
    phase: str = Field("unknown", max_length=64)
    category: str = Field("unknown", max_length=128)
    transport: str = Field("unknown", max_length=64)
    agent: Optional[str] = Field(None, max_length=256)


class CandidateCreate(BaseModel):
    """
    User-initiated or GLM-produced candidate artifact.
    For if_then_rule: trigger_dsl + action_type are required and validated.
    For hint: content is sufficient.
    """
    artifact_type: str = Field("hint", pattern="^(hint|if_then_rule|meta_guidance)$")
    action_type: str = Field("", max_length=128)
    content: str = Field(..., min_length=1, max_length=4000)
    trigger_dsl: str = Field("", max_length=1000)
    context_signature: str = Field("", max_length=512)
    observation: str = Field("", max_length=2000)
    why_it_matters: str = Field("", max_length=2000)
    risk_level: str = Field("low", pattern="^(low|medium|high)$")
    confidence: float = Field(0.7, ge=0.0, le=1.0)
    agent_id: str = Field("", max_length=256)
    domain: str = Field("", max_length=128)
    tags: list[str] = Field(default_factory=list)
    # Context fields (used to build context_signature if not provided explicitly)
    project: str = Field("", max_length=256)
    task_type: str = Field("", max_length=128)
    phase: str = Field("", max_length=64)
    category: str = Field("", max_length=128)
    transport: str = Field("mcp", max_length=64)


class RateRequest(BaseModel):
    useful: bool


class PromoteRequest(BaseModel):
    promoted_by: str = Field(..., max_length=256)
    promotion_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)


class DeferRequest(BaseModel):
    defer_days: Optional[int] = Field(None, ge=1, le=90)
    deferred_by: str = Field("user", min_length=1, max_length=256)
    defer_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=500)


class CandidateApproveRequest(BaseModel):
    approved_by: str = Field("user", min_length=1, max_length=256)
    approval_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)


class CandidateRejectRequest(BaseModel):
    rejected_by: str = Field("user", min_length=1, max_length=256)
    rejection_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)


class CandidateReport(BaseModel):
    id: str
    artifact_type: str
    action_type: str
    trigger_dsl: str
    content: str
    observation: str
    why_it_matters: str
    confidence: float
    evidence_count: int
    risk_level: str
    context_signature: str
    min_evidence: int
    tags: list[str]
    created_at: float
    display_language: Optional[str] = None
    display_content: Optional[str] = None
    display_observation: Optional[str] = None
    display_why_it_matters: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _resolve_context_signature(body: CandidateCreate) -> str:
    """Use explicit context_signature if provided, otherwise build from fields."""
    if body.context_signature:
        return body.context_signature
    if any([body.project, body.task_type, body.phase, body.category]):
        return make_context_signature(
            project=body.project or "unknown",
            task_type=body.task_type or "unknown",
            phase=body.phase or "unknown",
            category=body.category or "unknown",
            transport=body.transport or "unknown",
        )
    return ""


def _row_to_candidate_report(row: dict) -> CandidateReport:
    action_type = row.get("action_type") or ""
    defer_count = int(row.get("defer_count") or 0)
    return CandidateReport(
        id=row["id"],
        artifact_type=row.get("artifact_type") or "hint",
        action_type=action_type,
        trigger_dsl=row.get("trigger_dsl") or "",
        content=row.get("content") or "",
        observation=row.get("observation") or "",
        why_it_matters=row.get("why_it_matters") or "",
        confidence=round(float(row.get("confidence") or 0.7), 4),
        evidence_count=row.get("evidence_count") or 0,
        risk_level=row.get("risk_level") or "low",
        context_signature=row.get("context_signature") or "",
        min_evidence=min_evidence_for(action_type) + defer_count * 3,
        tags=row.get("tags") or [],
        created_at=float(row.get("created_at") or 0),
        display_language=row.get("display_language"),
        display_content=row.get("display_content"),
        display_observation=row.get("display_observation"),
        display_why_it_matters=row.get("display_why_it_matters"),
    )


# ── Events ─────────────────────────────────────────────────────────────────────

@router.post("/events", status_code=status.HTTP_201_CREATED)
async def record_event(body: EventCreate):
    """Record a canonical learning event."""
    if body.event_type not in CANONICAL_EVENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown event_type '{body.event_type}'; allowed: {sorted(CANONICAL_EVENT_TYPES)}",
        )
    store = get_learning_store()
    row_id = await store.write_event(
        event_type=body.event_type,
        agent_id=body.agent_id,
        project=body.project,
        transport=body.transport,
        episode_id=body.episode_id,
        context_signature=body.context_signature,
        payload=body.payload,
    )
    return {"id": row_id, "event_type": body.event_type}


@router.get("/events")
async def list_events(
    agent_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    episode_id: Optional[str] = Query(None),
    context_signature: Optional[str] = Query(None),
    since_hours: Optional[float] = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """List recent learning events."""
    import time
    since_ts = (time.time() - since_hours * 3600) if since_hours else None
    store = get_learning_store()
    rows = await store.list_events(
        agent_id=agent_id,
        event_type=event_type,
        episode_id=episode_id,
        context_signature=context_signature,
        since_ts=since_ts,
        limit=limit,
    )
    return {"events": rows, "total": len(rows)}


# ── Artifacts ──────────────────────────────────────────────────────────────────

@router.get("/artifacts")
async def list_artifacts(
    agent_id: Optional[str] = Query(None),
    artifact_type: Optional[str] = Query(None),
    scope: Optional[str] = Query(None),
    artifact_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=1000),
):
    """List artifacts with optional filters."""
    store = get_learning_store()
    rows = await store.list_artifacts(
        agent_id=agent_id,
        artifact_type=artifact_type,
        scope=scope,
        status=artifact_status,
        limit=limit,
    )
    rows = [hydrate_artifact_display_fields(row) for row in rows]
    return {
        "artifacts": rows,
        "total": len(rows),
    }


@router.post("/artifacts/{artifact_id}/rate")
async def rate_artifact(artifact_id: UUID, body: RateRequest):
    """Rate an artifact as useful or not."""
    store = get_learning_store()
    updated = await store.rate_artifact(artifact_id, body.useful)
    if updated is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return updated


@router.post("/artifacts/{artifact_id}/promote")
async def promote_artifact(artifact_id: UUID, body: PromoteRequest, background_tasks: BackgroundTasks):
    """Advance artifact scope one step (runtime_hint → persistent_rule → promoted_pattern)."""
    store = get_learning_store()
    before = await store.get_artifact(artifact_id)
    updated = await store.promote_artifact(
        artifact_id,
        body.promoted_by,
        promotion_source=body.promotion_source,
        promotion_reason=body.reason,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Artifact not found, already at max scope, or is a candidate (use /approve instead)",
        )

    # Emit an audit event so the dashboard shows something more useful than just counts.
    try:
        from app.services.event_emitter import emit
        from_scope = (before or {}).get("artifact_scope") or ""
        to_scope = updated.get("artifact_scope") if isinstance(updated, dict) else ""
        background_tasks.add_task(
            emit,
            "artifact_promoted",
            agent_id=(before or {}).get("agent_id") or body.promoted_by or "",
            project="",
            transport="api",
            episode_id="",
            context_signature=(before or {}).get("context_signature") or "learning/promote",
            payload={
                "artifact_id": str(artifact_id),
                "from_scope": from_scope,
                "to_scope": to_scope,
                "artifact_type": (before or {}).get("artifact_type") or "",
                "action_type": (before or {}).get("action_type") or "",
                "promoted_by": body.promoted_by,
                "promotion_source": body.promotion_source,
                "reason": body.reason,
            },
        )
    except Exception:
        pass
    return updated


# ── Candidates & Report ────────────────────────────────────────────────────────

@router.get("/report", response_model=list[CandidateReport])
async def get_report(limit: int = Query(10, ge=1, le=100)):
    """
    Return top candidates ready for human review.

    Filters applied:
    - scope=candidate, status=pending_review
    - next_surface_after <= now
    - evidence_count >= min_evidence for the candidate's action_type

    Ranked by confidence * evidence_count DESC.
    """
    store = get_learning_store()
    rows = await store.get_report_candidates(limit=limit)
    rows = [hydrate_artifact_display_fields(r) for r in rows]
    return [_row_to_candidate_report(r) for r in rows]


@router.get("/artifacts/{artifact_id}/translate")
async def translate_artifact(artifact_id: UUID, language: Optional[str] = Query(None)):
    """Translate one artifact on demand without replacing the original."""
    store = get_learning_store()
    row = await store.get_artifact(artifact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    translated = await translate_artifact_fields(
        content=row.get("content") or "",
        observation=row.get("observation") or "",
        why_it_matters=row.get("why_it_matters") or "",
        target_language=language,
    )
    return {
        "id": str(artifact_id),
        "language": translated["language"],
        "original": translated["original_content"],
        "original_observation": translated["original_observation"],
        "original_why_it_matters": translated["original_why_it_matters"],
        "translated": translated["translated_content"],
        "translated_observation": translated["translated_observation"],
        "translated_why_it_matters": translated["translated_why_it_matters"],
    }


@router.post("/candidates", status_code=status.HTTP_201_CREATED)
async def create_candidate(body: CandidateCreate):
    """
    Create a candidate artifact (user-initiated improvement or GLM-produced candidate).

    - For if_then_rule: trigger_dsl + action_type are validated against DSL whitelist.
    - Dedup: if a candidate with the same key already exists, evidence_count is incremented.
    - Records a user_request event for auditability.
    """
    # Validate DSL if if_then_rule
    if body.artifact_type == "if_then_rule":
        errors = validate_if_then_rule(body.trigger_dsl, body.action_type)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"dsl_errors": errors},
            )
    elif body.action_type and body.action_type not in ALLOWED_ACTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown action_type '{body.action_type}'; allowed: {sorted(ALLOWED_ACTION_TYPES)}",
        )

    ctx_sig = _resolve_context_signature(body)
    store = get_learning_store()

    cleaned_fields, enriched_meta = await prepare_artifact_texts(
        content=body.content,
        observation=body.observation,
        why_it_matters=body.why_it_matters,
    )

    artifact_id, created = await store.upsert_candidate(
        agent_id=body.agent_id,
        action_type=body.action_type,
        content=cleaned_fields["content"],
        trigger_dsl=body.trigger_dsl,
        context_signature=ctx_sig,
        observation=cleaned_fields["observation"],
        why_it_matters=cleaned_fields["why_it_matters"],
        risk_level=body.risk_level,
        confidence=body.confidence,
        tags=body.tags,
        domain=body.domain,
        artifact_type=body.artifact_type,
        meta=enriched_meta,
    )

    # Record user_request event for auditability
    await store.write_event(
        event_type="user_request",
        agent_id=body.agent_id,
        project=body.project or "",
        transport=body.transport,
        context_signature=ctx_sig,
        payload={
            "request_type": "other",
            "proposal_type": "learning_candidate",
            "proposal": {
                "artifact_type": body.artifact_type,
                "action_type": body.action_type,
                "artifact_id": str(artifact_id),
                "created": created,
            },
        },
    )

    row = await store.get_artifact(artifact_id)
    return {
        "id": str(artifact_id),
        "created": created,
        "evidence_count": row.get("evidence_count") if row else 1,
        "key": make_artifact_key(body.action_type, body.trigger_dsl, ctx_sig),
    }


@router.post("/candidates/{artifact_id}/approve")
async def approve_candidate(
    artifact_id: UUID,
    background_tasks: BackgroundTasks,
    body: CandidateApproveRequest | None = None,
):
    """
    Approve a candidate: promotes scope to runtime_hint (status=active).
    For crystallize_knowledge candidates, also creates a canonical memory in Qdrant.
    Records a positive feedback signal.
    """
    from app.services.event_emitter import emit
    store = get_learning_store()
    approval = body or CandidateApproveRequest()

    # Fetch before approval to check action_type and metadata
    artifact = await store.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate not found or not in candidate scope",
        )

    canonical_memory_id: str | None = None
    updated = await store.approve_candidate(
        artifact_id,
        approved_by=approval.approved_by,
        approval_source=approval.approval_source,
        approval_reason=approval.reason,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate not found or not in candidate scope",
        )

    if artifact.get("action_type") == "crystallize_knowledge":
        from app.dependencies import get_qdrant, get_ollama
        from app.config import settings as _settings
        from app.services.crystallization_service import apply_crystallization, CrystallizationCandidate

        meta = artifact.get("meta") or {}
        try:
            qdrant = get_qdrant()
            ollama = get_ollama()
            crystal_candidate = CrystallizationCandidate(
                key=meta.get("candidate_key", str(artifact_id)[:16]),
                topic_path=meta.get("topic_path", artifact.get("context_signature", "")),
                target_scope=meta.get("target_scope", "domain"),
                statement=artifact.get("content", ""),
                observation=artifact.get("observation", ""),
                why_it_matters=artifact.get("why_it_matters", ""),
                supports=meta.get("supports", []),
                confidence=float(artifact.get("confidence", 0.7)),
                project_diversity=int(meta.get("project_diversity", 1)),
                evidence_count=int(meta.get("evidence_count", 1)),
            )
            canonical_memory_id = await apply_crystallization(
                crystal_candidate, qdrant._client, _settings.qdrant_collection_name, ollama
            )
        except Exception as _cryst_exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "crystallize_knowledge approve: failed to create canonical memory after candidate approval: %s",
                _cryst_exc,
            )

    await store.write_feedback(
        artifact_id=str(artifact_id),
        valence="positive",
        magnitude=1.0,
        source="user",
        payload={
            "action": "approve",
            "approved_by": approval.approved_by,
            "approval_source": approval.approval_source,
            "reason": approval.reason,
        },
    )
    background_tasks.add_task(emit, "candidate_approved",
        agent_id=approval.approved_by,
        payload={"artifact_id": str(artifact_id),
                 "action_type": updated.get("action_type", ""),
                 "artifact_type": updated.get("artifact_type", ""),
                 "canonical_memory_id": canonical_memory_id,
                 "approval_source": approval.approval_source,
                 "reason": approval.reason})
    result = dict(updated)
    if canonical_memory_id:
        result["canonical_memory_id"] = canonical_memory_id
    return result


@router.post("/candidates/{artifact_id}/reject")
async def reject_candidate(
    artifact_id: UUID,
    background_tasks: BackgroundTasks,
    body: CandidateRejectRequest | None = None,
):
    """
    Reject a candidate: archives it and records a negative feedback signal.
    """
    from app.services.event_emitter import emit
    store = get_learning_store()
    review = body or CandidateRejectRequest()
    updated = await store.reject_candidate(
        artifact_id,
        rejected_by=review.rejected_by,
        rejection_source=review.rejection_source,
        rejection_reason=review.reason,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    await store.write_feedback(
        artifact_id=str(artifact_id),
        valence="negative",
        magnitude=1.0,
        source="user",
        payload={
            "action": "reject",
            "rejected_by": review.rejected_by,
            "rejection_source": review.rejection_source,
            "reason": review.reason,
        },
    )
    background_tasks.add_task(emit, "candidate_rejected",
        agent_id=review.rejected_by,
        payload={"artifact_id": str(artifact_id),
                 "action_type": updated.get("action_type", ""),
                 "artifact_type": updated.get("artifact_type", ""),
                 "rejection_source": review.rejection_source,
                 "reason": review.reason})
    return updated


@router.post("/candidates/{artifact_id}/defer")
async def defer_candidate(artifact_id: UUID, body: DeferRequest):
    """
    Defer a candidate for later review.

    Effects:
    - evidence_count threshold raised by +3 (must accumulate more signal)
    - next_surface_after = now + effective_days (doubles each defer: 7→14→28→...→90)
    - Auto-archived when effective_days reaches 90
    """
    store = get_learning_store()
    updated = await store.defer_candidate(
        artifact_id,
        defer_days=body.defer_days,
        deferred_by=body.deferred_by,
        defer_source=body.defer_source,
        defer_reason=body.reason,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {
        "id": str(artifact_id),
        "defer_count": updated.get("defer_count"),
        "next_surface_after": updated.get("next_surface_after"),
        "status": updated.get("status"),
        "evidence_count": updated.get("evidence_count"),
        "reason": body.reason,
        "deferred_by": body.deferred_by,
        "defer_source": body.defer_source,
    }


# ── Model Mirror ───────────────────────────────────────────────────────────────

@router.post("/mirror/run")
async def run_model_mirror():
    """
    Manually trigger a model mirror analysis cycle.

    Analyzes recent events, generates up to 2-3 candidate artifacts for human review.
    Returns a summary of what was found and created/updated.
    """
    from app.dependencies import get_ollama
    from app.services.model_mirror import get_model_mirror

    store = get_learning_store()
    try:
        ollama = get_ollama()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ollama service not available",
        )

    mirror = get_model_mirror()
    result = await mirror.run(ollama, store)
    return result.to_dict()


@router.get("/mirror/status")
async def model_mirror_status():
    """Return the result of the last model mirror run."""
    from app.services.model_mirror import get_model_mirror, MODEL_MIRROR_INTERVAL_HOURS

    mirror = get_model_mirror()
    last = mirror.last_result()
    return {
        "last_run": last.to_dict() if last else None,
        "interval_hours": MODEL_MIRROR_INTERVAL_HOURS,
        "next_run_at": mirror.next_run_at,
    }


# ── Scout ──────────────────────────────────────────────────────────────────────

class ScoutCheckRequest(BaseModel):
    project: str = Field("", max_length=256)
    task: str = Field(..., min_length=1, max_length=1000)
    agent_id: str = Field("", max_length=256)
    context_signature: str = Field("", max_length=512)
    since_hours: float = Field(168.0, ge=1.0, description="Look-back window for skill-gap signals (hours)")


class ScoutFetchRequest(BaseModel):
    project: str = Field("", max_length=256)
    task: str = Field(..., min_length=1, max_length=1000)
    domains: list[str] = Field(default_factory=list, description="Missing skill domains to focus on")
    agent_id: str = Field("", max_length=256)
    context_signature: str = Field("", max_length=512)


@router.post("/scout/check")
async def scout_check(body: ScoutCheckRequest):
    """
    Sufficiency gate: checks if current active rules cover the task/project.

    Returns sufficient=false if fewer than SCOUT_MIN_ARTIFACTS active rules exist
    or missing_skill signals were detected in recent events.
    """
    from app.services.best_practice_scout import check_sufficiency

    store = get_learning_store()
    result = await check_sufficiency(
        store=store,
        project=body.project,
        task=body.task,
        agent_id=body.agent_id,
        context_signature=body.context_signature,
        since_hours=body.since_hours,
    )
    return {
        "sufficient": result.sufficient,
        "active_count": result.active_count,
        "missing_skill_count": result.missing_skill_count,
        "missing_domains": result.missing_domains,
        "reason": result.reason,
        "confidence": result.confidence,
    }


@router.post("/scout/fetch", status_code=status.HTTP_201_CREATED)
async def scout_fetch(body: ScoutFetchRequest, background_tasks: BackgroundTasks):
    """
    Fetch best practices from LLM and create up to 3 candidates for human review.

    Each candidate:
    - artifact_type=meta_guidance, action_type=suggest_save_result
    - tags: ["external", "best-practice", <domain>]
    - meta: {source: "external_llm", title, task, domains, when_not_to_use}

    Dedup is handled by upsert_candidate: same task+domain combo updates evidence_count.
    After review, approve → persistent_rule; reject → 14-day cooldown.
    """
    from app.dependencies import get_ollama
    from app.services.best_practice_scout import fetch_best_practices
    from app.services.event_emitter import emit

    try:
        ollama = get_ollama()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ollama not available",
        )

    practices = await fetch_best_practices(
        ollama=ollama,
        project=body.project,
        task=body.task,
        domains=body.domains,
    )

    if not practices:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM did not return parseable best practices. Check Ollama logs.",
        )

    store = get_learning_store()
    created_candidates = []

    for bp in practices:
        content_parts = [bp.what_and_why]
        if bp.example:
            content_parts.append(f"\nПример:\n{bp.example}")
        if bp.expected_result:
            content_parts.append(f"\nОжидаемый результат: {bp.expected_result}")
        if bp.when_not_to_use:
            content_parts.append(f"\nКогда не использовать: {bp.when_not_to_use}")
        content = "\n".join(content_parts)

        observation_parts = []
        if bp.pros:
            observation_parts.append(f"Плюсы:\n{bp.pros}")
        if bp.cons:
            observation_parts.append(f"Минусы:\n{bp.cons}")
        observation = "\n\n".join(observation_parts)

        tags = ["external", "best-practice"]
        if bp.domain:
            tags.append(bp.domain)

        # Use title+project as part of the context_signature for dedup
        ctx_sig = body.context_signature or make_context_signature(
            project=body.project or "unknown",
            task_type=bp.domain or "general",
            phase="scout",
            category="best-practice",
            transport="api",
        )

        cleaned_fields, enriched_meta = await prepare_artifact_texts(
            content=content,
            observation=observation,
            why_it_matters=bp.what_and_why,
            meta={
                "source": "external_llm",
                "title": bp.title,
                "task": body.task,
                "domains": body.domains,
                "when_not_to_use": bp.when_not_to_use,
            },
        )

        artifact_id, created = await store.upsert_candidate(
            agent_id=body.agent_id or "scout",
            action_type="suggest_save_result",
            content=cleaned_fields["content"],
            context_signature=ctx_sig,
            observation=cleaned_fields["observation"],
            why_it_matters=cleaned_fields["why_it_matters"],
            risk_level="low",
            confidence=0.65,
            tags=tags,
            domain=bp.domain,
            artifact_type="meta_guidance",
            meta=enriched_meta,
        )
        created_candidates.append({
            "id": str(artifact_id),
            "title": bp.title,
            "domain": bp.domain,
            "created": created,
        })

    background_tasks.add_task(
        emit,
        "external_best_practice_requested",
        agent_id=body.agent_id or "scout",
        project=body.project,
        transport="api",
        episode_id="",
        context_signature=body.context_signature,
        payload={
            "task": body.task,
            "domains": body.domains,
            "candidates_created": len([c for c in created_candidates if c["created"]]),
            "candidates_updated": len([c for c in created_candidates if not c["created"]]),
        },
    )

    return {"candidates": created_candidates, "total": len(created_candidates)}


# ── Hint two-step review flow ──────────────────────────────────────────────────

class HintReactRequest(BaseModel):
    accept: bool
    reason: str = ""


@router.get("/hints/{artifact_id}")
async def get_hint(artifact_id: UUID):
    """
    Step 1 of hint review: return full content of a scout best-practice candidate.

    The agent shows this to the user BEFORE asking for accept/reject.
    Response includes: title, domain, what_and_why, example, expected_result, pros, cons, when_not_to_use.
    """
    store = get_learning_store()
    rows = await store.list_artifacts(limit=1)
    # Direct lookup by id
    def _get_row(sid: str) -> dict | None:
        import sqlite3 as _sq
        conn = store._conn  # type: ignore[attr-defined]
        with store._lock:  # type: ignore[attr-defined]
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (sid,)).fetchone()
        if not row:
            return None
        from app.services.learning_store import _row_to_dict
        return _row_to_dict(dict(row))

    row = await store._run_sync(lambda: _get_row(str(artifact_id)))  # type: ignore[attr-defined]
    if not row:
        raise HTTPException(status_code=404, detail="Hint not found")
    if "external" not in (row.get("tags") or []):
        raise HTTPException(status_code=400, detail="Not a scout hint")

    meta = row.get("meta") or {}
    return {
        "id": row["id"],
        "title": meta.get("title") or row.get("content", "")[:60],
        "domain": row.get("domain") or "",
        "content": row.get("content") or "",
        "observation": row.get("observation") or "",
        "when_not_to_use": meta.get("when_not_to_use") or "",
        "status": row.get("status") or "",
    }


@router.post("/hints/{artifact_id}/react")
async def react_to_hint(artifact_id: UUID, body: HintReactRequest, background_tasks: BackgroundTasks):
    """
    Step 2 of hint review: accept or reject a scout best-practice candidate.

    - accept=true  → approve + promote to persistent_rule
    - accept=false → reject (cooldown, won't resurface)
    """
    from app.services.event_emitter import emit
    store = get_learning_store()
    if body.accept:
        updated = await store.approve_candidate(
            artifact_id,
            approved_by="user",
            approval_source="hint_review_accept",
            approval_reason=body.reason,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Hint not found")
        await store.write_feedback(
            artifact_id=str(artifact_id),
            valence="positive",
            magnitude=1.0,
            source="user",
            payload={"action": "hint_accept", "reason": body.reason},
        )
        # Promote to persistent_rule immediately (scout-accepted practices are project rules)
        try:
            from app.routers.learning import promote_artifact, PromoteRequest
            await promote_artifact(
                artifact_id,
                PromoteRequest(
                    promoted_by="user",
                    promotion_source="hint_review_accept",
                    reason=body.reason,
                ),
                background_tasks,
            )
        except Exception:
            pass
        background_tasks.add_task(emit, "hint_accepted",
            agent_id="user",
            payload={"artifact_id": str(artifact_id), "reason": body.reason})
        return {"status": "accepted", "id": str(artifact_id)}
    else:
        updated = await store.reject_candidate(artifact_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Hint not found")
        await store.write_feedback(
            artifact_id=str(artifact_id),
            valence="negative",
            magnitude=1.0,
            source="user",
            payload={"action": "hint_reject", "reason": body.reason},
        )
        background_tasks.add_task(emit, "hint_rejected",
            agent_id="user",
            payload={"artifact_id": str(artifact_id), "reason": body.reason})
        return {"status": "rejected", "id": str(artifact_id)}


# ── Utilities ──────────────────────────────────────────────────────────────────

@router.post("/context-signature")
async def build_context_signature(body: ContextSignatureRequest):
    """Build a deterministic context_signature from structured fields."""
    sig = make_context_signature(
        project=body.project,
        task_type=body.task_type,
        phase=body.phase,
        category=body.category,
        transport=body.transport,
        agent=body.agent,
    )
    return {"context_signature": sig}
