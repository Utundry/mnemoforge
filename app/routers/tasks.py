"""
Background Task Queue — REST API.

Agents and humans submit long-running LLM jobs here instead of waiting
for slow synchronous endpoints to complete.

Endpoints:
  GET  /tasks            — list recent jobs (filterable by type/status)
  GET  /tasks/{job_id}   — poll a specific job for status + result
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import JobQueueDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])

_STATUS_VALUES = {"queued", "running", "done", "failed"}
_LANE_VALUES = {"fast", "slow"}


class MemoryScribeCompactRequest(BaseModel):
    project: str = Field("supermemory", min_length=1, max_length=128)
    task_id: str = Field("", max_length=256)
    task_title: str = Field("", max_length=500)
    stage: str = Field("in_progress", pattern="^(planning|in_progress|blocked|interrupted|handoff|completed)$")
    status: str = Field("active", pattern="^(planning|active|paused|done)$")
    raw_notes: str = Field(..., min_length=1, max_length=20000)
    reason: str = Field("memory_scribe_compact", max_length=500)
    use_llm: bool = Field(True, description="Use cheap local/cloud LLM extraction when available; deterministic fallback is still used on failure.")
    mode: str = Field("economy", pattern="^(economy|strict_economy|balanced)$")
    model_context_window: int = Field(32000, ge=1000)
    resume_budget_ratio: float | None = Field(None, ge=0.001, le=0.5)
    resume_budget_profile: str = Field("normal", pattern="^(normal|complex|handoff|emergency)$")


@router.post("/memory-scribe/compact", status_code=202)
async def submit_memory_scribe_compact(body: MemoryScribeCompactRequest, queue: JobQueueDep) -> dict:
    """
    Queue a low-cost memory-scribe job that turns raw work notes into a
    reviewable task checkpoint draft. The job does not mutate project memory.
    """
    job_id = await queue.submit("memory_scribe_compact", body.model_dump())
    return {
        "job_id": job_id,
        "job_type": "memory_scribe_compact",
        "status": "queued",
        "poll": f"/tasks/{job_id}",
        "mutates_memory": False,
    }


@router.post("/memory-scribe/draft-task-checkpoint", status_code=202)
async def submit_draft_task_checkpoint(body: MemoryScribeCompactRequest, queue: JobQueueDep) -> dict:
    """
    Queue a low-cost memory-scribe job that returns record_task_checkpoint args.
    The job is review-only and does not mutate project memory.
    """
    payload = body.model_dump()
    payload["reason"] = payload.get("reason") or "draft_task_checkpoint"
    job_id = await queue.submit("draft_task_checkpoint", payload)
    return {
        "job_id": job_id,
        "job_type": "draft_task_checkpoint",
        "status": "queued",
        "poll": f"/tasks/{job_id}",
        "mutates_memory": False,
        "recommended_next_tool": "record_task_checkpoint",
    }


@router.get("")
async def list_jobs(
    queue: JobQueueDep,
    job_type: Optional[str] = Query(None, description="Filter by job type, e.g. 'project_ingest'"),
    status: Optional[str] = Query(None, description="Filter by status: queued|running|done|failed"),
    lane: Optional[str] = Query(None, description="Filter by queue lane: fast|slow"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """List recent background jobs, newest first."""
    if status and status not in _STATUS_VALUES:
        raise HTTPException(400, f"Invalid status '{status}'. Use: {sorted(_STATUS_VALUES)}")
    if lane and lane not in _LANE_VALUES:
        raise HTTPException(400, f"Invalid lane '{lane}'. Use: {sorted(_LANE_VALUES)}")
    jobs = queue.list_jobs(job_type=job_type, status=status, lane=lane, limit=limit)
    return {"count": len(jobs), "jobs": jobs}


@router.get("/{job_id}")
async def get_job(job_id: str, queue: JobQueueDep) -> dict:
    """
    Poll a background job for status and result.

    status values:
      queued   — waiting in queue
      running  — LLM processing in progress
      done     — completed successfully, result is populated
      failed   — error occurred, error field has details
    """
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return job
