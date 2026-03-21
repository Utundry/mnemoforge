"""
Model Registry API — quota management and cross-CLI task handoff.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep
from app.services.model_registry import get_model_registry

router = APIRouter(prefix="/models", tags=["models"])


# ── Request / Response models ────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=256)
    provider: str = Field(..., min_length=1, max_length=64)
    daily_limit: int = Field(..., gt=0)
    limit_unit: str = Field("tokens", pattern="^(tokens|requests)$")
    priority: int = Field(99, ge=1)
    task_capabilities: list[str] = Field(default_factory=list)
    initial_scores: dict[str, float] = Field(default_factory=dict)
    weekly_limit: Optional[int] = Field(None, gt=0, description="Optional weekly quota override (same units as daily limit)")


class ReportUsageRequest(BaseModel):
    model_id: str
    units_used: int = Field(..., gt=0)


class ReportLimitRequest(BaseModel):
    model_id: str
    error_code: Optional[str] = None
    error_msg: Optional[str] = None
    retry_after: Optional[int] = Field(None, description="Cooldown seconds until model is available again")


class HandoffRequest(BaseModel):
    task_id: Optional[str] = Field(None, description="Unique task identifier (auto-generated if omitted)")
    from_agent: str = Field(..., description="Source CLI: claude-code | codex | cline | gemini-cli")
    to_agent: str = Field(..., description="Target CLI: claude-code | codex | cline | gemini-cli")
    task_description: str = Field(..., min_length=1, max_length=2000)
    partial_result: Optional[str] = Field(None, max_length=5000)
    key_facts: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field("manual", description="manual | limit_hit")
    agent_id: str = Field("handoff", description="Memory agent_id for Qdrant storage")


class PickupRequest(BaseModel):
    agent_id: str = Field(..., description="This CLI's identity — matches to_agent in stored handoffs")
    limit: int = Field(3, ge=1, le=20)


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/register")
async def register_model(body: RegisterRequest):
    """Register or update a cloud model with quota config."""
    reg = get_model_registry()
    quota = reg.register(
        model_id=body.model_id,
        display_name=body.display_name,
        provider=body.provider,
        daily_limit=body.daily_limit,
        limit_unit=body.limit_unit,
        priority=body.priority,
        task_capabilities=body.task_capabilities,
        initial_scores=body.initial_scores,
        weekly_limit=body.weekly_limit,
    )
    return {"registered": True, "model_id": quota.model_id, "daily_limit": quota.daily_limit}


@router.get("/available")
async def list_available(task_type: Optional[str] = None):
    """List available models ranked by priority. Filter by task_type capability."""
    reg = get_model_registry()
    quotas = reg.available(task_type=task_type)
    return [
        {
            "model_id": q.model_id,
            "display_name": q.display_name,
            "provider": q.provider,
            "remaining": q.remaining,
            "remaining_pct": round(q.remaining_fraction * 100, 1),
            "limit_unit": q.limit_unit,
            "priority": q.priority,
            "is_available": q.is_available,
            "task_capabilities": q.task_capabilities,
        }
        for q in quotas
        if q.is_available
    ]


@router.get("/status")
async def quota_status():
    """Quota dashboard for all registered models."""
    return get_model_registry().status_dashboard()


@router.get("/handoff_log")
async def get_handoff_log(limit: int = 20):
    """Recent cross-CLI handoff events."""
    return get_model_registry().handoff_log(limit=limit)


@router.post("/report_usage")
async def report_usage(body: ReportUsageRequest):
    """Record units consumed. Updates remaining quota."""
    reg = get_model_registry()
    if body.model_id not in reg._models:
        raise HTTPException(404, f"Model '{body.model_id}' not registered")
    quota = reg.record_usage(body.model_id, body.units_used)
    return {
        "model_id": quota.model_id,
        "used_today": quota.used_today,
        "remaining": quota.remaining,
        "is_available": quota.is_available,
    }


@router.post("/report_limit")
async def report_limit(body: ReportLimitRequest):
    """Mark model as rate-limited / quota-exhausted. Triggers cooldown."""
    reg = get_model_registry()
    if body.model_id not in reg._models:
        raise HTTPException(404, f"Model '{body.model_id}' not registered")
    quota = reg.report_limit_hit(
        model_id=body.model_id,
        error_code=body.error_code,
        error_msg=body.error_msg,
        retry_after=body.retry_after,
    )
    return {
        "model_id": quota.model_id,
        "is_available": quota.is_available,
        "cooldown_until": quota.cooldown_until,
    }


@router.post("/handoff")
async def create_handoff(body: HandoffRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Package task context in supermemory for pickup by target CLI.

    Stores compact handoff context in Qdrant (category='handoff', status='pending').
    Returns memory_id + next available models.
    """
    task_id = body.task_id or str(uuid.uuid4())[:8]

    # Build compact handoff content
    facts_text = "\n".join(f"- {f}" for f in body.key_facts[:10]) if body.key_facts else ""
    content_parts = [
        "HANDOFF CONTEXT",
        f"task_id: {task_id}",
        f"from_agent: {body.from_agent}",
        f"to_agent: {body.to_agent}",
        f"reason: {body.reason}",
        f"task: {body.task_description[:500]}",
    ]
    if body.partial_result:
        content_parts.append(f"partial_result: {body.partial_result[:1000]}")
    if facts_text:
        content_parts.append(f"key_facts:\n{facts_text}")
    content = "\n".join(content_parts)

    # Embed and store in Qdrant
    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType
    vector = await ollama.embed(content)
    mem = MemoryCreate(
        content=content,
        agent_id=body.agent_id,
        memory_type=MemoryType.context,
        category="handoff",
        importance_score=0.9,
        source=f"handoff:{body.from_agent}",
        tags=[f"to:{body.to_agent}", f"from:{body.from_agent}", task_id, body.reason],
        session_id=task_id,
    )
    memory_id = await qdrant.insert(mem, vector)
    # Mark as pending so pickup_handoff can find it
    await qdrant.mark_handoff_pending(memory_id)

    # Log handoff in SQLite
    reg = get_model_registry()
    reg.log_handoff(
        task_id=task_id,
        from_agent=body.from_agent,
        to_agent=body.to_agent,
        memory_id=str(memory_id),
        reason=body.reason,
    )

    # Auto-mark from_model as limit-hit if reason is limit_hit
    if body.reason == "limit_hit" and body.from_agent in reg._models:
        reg.report_limit_hit(body.from_agent)

    # Get next available models ranked for any task
    ranked = reg.rank_for_task("code_generation")  # generic ranking
    next_available = [
        {"model_id": m, "score": round(s, 3)}
        for m, s in ranked[:3]
        if m != body.from_agent
    ]

    return {
        "memory_id": str(memory_id),
        "task_id": task_id,
        "from_agent": body.from_agent,
        "to_agent": body.to_agent,
        "next_available": next_available,
        "pickup_instruction": f"In {body.to_agent}: use pickup_handoff(agent_id='{body.to_agent}')",
    }


@router.post("/handoff/pickup")
async def pickup_handoff(body: PickupRequest, qdrant: QdrantDep):
    """
    Retrieve pending handoffs addressed to this agent/CLI.
    Updates their status to 'picked_up' to prevent double-pickup.
    """
    handoffs = await qdrant.get_pending_handoffs(to_agent=body.agent_id, limit=body.limit)
    results = []
    for h in handoffs:
        await qdrant.mark_handoff_picked_up(h["memory_id"])
        results.append(h)
    return {
        "agent_id": body.agent_id,
        "found": len(results),
        "handoffs": results,
    }


@router.get("/{model_id}")
async def get_model(model_id: str):
    """Get quota state for a single model."""
    reg = get_model_registry()
    try:
        quota = reg.get_model(model_id)
    except KeyError:
        raise HTTPException(404, f"Model '{model_id}' not registered")
    return {
        "model_id": quota.model_id,
        "display_name": quota.display_name,
        "provider": quota.provider,
        "daily_limit": quota.daily_limit,
        "limit_unit": quota.limit_unit,
        "used_today": quota.used_today,
        "remaining": quota.remaining,
        "remaining_pct": round(quota.remaining_fraction * 100, 1),
        "priority": quota.priority,
        "task_capabilities": quota.task_capabilities,
        "is_available": quota.is_available,
        "cooldown_until": quota.cooldown_until,
    }


@router.delete("/{model_id}/reset")
async def reset_quota(model_id: str):
    """Reset today's quota to 0 and clear cooldown (admin/testing)."""
    reg = get_model_registry()
    if model_id not in reg._models:
        raise HTTPException(404, f"Model '{model_id}' not registered")
    quota = reg.reset_quota(model_id)
    return {"model_id": quota.model_id, "used_today": quota.used_today, "is_available": quota.is_available}
