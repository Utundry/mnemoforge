from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.performance_tracker import get_tracker

router = APIRouter(prefix="/tracker", tags=["tracker"])


class RecordRequest(BaseModel):
    component: str = Field(..., description="'qwen3:1.7b', 'cloud-llm', 'skill:fix-layout'")
    task_type: str = Field(..., description="'layout_fix', 'code_generation', ...")
    success: bool
    latency_ms: Optional[float] = None
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    metadata: Optional[dict] = None
    corrected_task_type: Optional[str] = Field(
        None,
        description=(
            "Actual task type when the specialist found the classification wrong. "
            "'Ivanov feedback': this task belongs to corrected_task_type, not task_type. "
            "Used to improve future routing decisions."
        ),
    )


@router.post("/record")
async def record_event(body: RecordRequest):
    """Record a task execution outcome.

    Set corrected_task_type when the executing component (cloud LLM) determined
    the routed task_type was incorrect — signalling the next candidate for similar tasks.
    """
    event_id = get_tracker().record(
        component=body.component,
        task_type=body.task_type,
        success=body.success,
        latency_ms=body.latency_ms,
        agent_id=body.agent_id,
        session_id=body.session_id,
        metadata=body.metadata,
        corrected_task_type=body.corrected_task_type,
    )
    result = {
        "event_id": event_id,
        "component": body.component,
        "task_type": body.task_type,
        "success": body.success,
    }
    if body.corrected_task_type:
        result["corrected_task_type"] = body.corrected_task_type
        result["correction_note"] = (
            f"Correction recorded: '{body.task_type}' → '{body.corrected_task_type}'. "
            "Future routing for similar tasks will consider this signal."
        )
    return result


@router.get("/stats")
async def get_stats(
    component: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
    since_hours: Optional[float] = Query(None, description="Limit to last N hours"),
):
    """Aggregate success rates and latencies per component+task_type."""
    return get_tracker().stats(component=component, task_type=task_type, since_hours=since_hours)


@router.get("/history")
async def get_history(
    limit: int = Query(50, ge=1, le=500),
    component: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
):
    """Recent task events."""
    return get_tracker().history(limit=limit, component=component, task_type=task_type)


@router.get("/corrections")
async def get_corrections(
    task_type: Optional[str] = Query(None, description="Filter by original classified task_type"),
    min_count: int = Query(1, ge=1, description="Minimum number of corrections to include"),
    since_hours: Optional[float] = Query(None, description="Limit to last N hours"),
):
    """Return task_type correction signals from specialist feedback.

    Shows where Uncle Petya's classification was wrong and what Ivanov said it actually was.
    Use this to understand systematic misrouting patterns.

    Example response:
      classified_as='text_summarization', actual_type='code_review', count=5, correction_rate=0.25
      → 25% of tasks classified as text_summarization were actually code_review
    """
    return get_tracker().corrections(
        task_type=task_type,
        min_count=min_count,
        since_hours=since_hours,
    )


@router.get("/trends")
async def get_trends(
    component: str = Query(...),
    task_type: str = Query(...),
    buckets: int = Query(10, ge=2, le=50),
):
    """Rolling success rate over time for a component+task_type."""
    return get_tracker().trends(component=component, task_type=task_type, buckets=buckets)
