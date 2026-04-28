from pathlib import Path

import pytest

from app.services.job_queue import JobQueue


@pytest.mark.asyncio
async def test_job_queue_starts_configured_worker_count(monkeypatch):
    monkeypatch.setenv("JOB_QUEUE_WORKERS", "2")
    queue = JobQueue(Path(":memory:"))
    try:
        await queue.start()
        assert len(queue._worker_tasks) == 2
        assert queue._worker_task is queue._worker_tasks[0]
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_job_queue_routes_slow_jobs_to_slow_lane(monkeypatch):
    monkeypatch.setenv("JOB_QUEUE_WORKERS", "2")
    queue = JobQueue(Path(":memory:"))
    try:
        job_id = await queue.submit("skills_retag", {"limit": 10})
        job = queue.get_job(job_id)
        assert job is not None
        assert job["lane"] == "slow"
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_job_queue_routes_memory_scribe_to_slow_lane(monkeypatch):
    monkeypatch.setenv("JOB_QUEUE_WORKERS", "2")
    queue = JobQueue(Path(":memory:"))
    try:
        job_id = await queue.submit("memory_scribe_compact", {"raw_notes": "Summary: draft"})
        job = queue.get_job(job_id)
        assert job is not None
        assert job["lane"] == "slow"
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_job_queue_routes_draft_task_checkpoint_to_slow_lane(monkeypatch):
    monkeypatch.setenv("JOB_QUEUE_WORKERS", "2")
    queue = JobQueue(Path(":memory:"))
    try:
        job_id = await queue.submit("draft_task_checkpoint", {"raw_notes": "Summary: draft"})
        job = queue.get_job(job_id)
        assert job is not None
        assert job["lane"] == "slow"
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_job_queue_routes_fast_jobs_to_fast_lane(monkeypatch):
    monkeypatch.setenv("JOB_QUEUE_WORKERS", "2")
    queue = JobQueue(Path(":memory:"))
    try:
        job_id = await queue.submit("data_hygiene_apply_exclusion", {"limit": 10})
        job = queue.get_job(job_id)
        assert job is not None
        assert job["lane"] == "fast"
    finally:
        await queue.stop()
