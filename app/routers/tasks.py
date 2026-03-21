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

from app.services.job_queue import get_job_queue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])

_STATUS_VALUES = {"queued", "running", "done", "failed"}


@router.get("")
async def list_jobs(
    job_type: Optional[str] = Query(None, description="Filter by job type, e.g. 'project_ingest'"),
    status: Optional[str] = Query(None, description="Filter by status: queued|running|done|failed"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """List recent background jobs, newest first."""
    if status and status not in _STATUS_VALUES:
        raise HTTPException(400, f"Invalid status '{status}'. Use: {sorted(_STATUS_VALUES)}")
    jobs = get_job_queue().list_jobs(job_type=job_type, status=status, limit=limit)
    return {"count": len(jobs), "jobs": jobs}


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    """
    Poll a background job for status and result.

    status values:
      queued   — waiting in queue
      running  — LLM processing in progress
      done     — completed successfully, result is populated
      failed   — error occurred, error field has details
    """
    job = get_job_queue().get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return job
