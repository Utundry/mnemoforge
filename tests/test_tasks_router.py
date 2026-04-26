from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import dependencies
from app.main import create_app


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.jobs = [
            {
                "id": "job-fast",
                "job_type": "data_hygiene_apply_exclusion",
                "lane": "fast",
                "status": "queued",
                "payload": {"finding_ids": ["h-1"]},
            },
            {
                "id": "job-slow",
                "job_type": "docs_rebuild",
                "lane": "slow",
                "status": "running",
                "payload": {"project": "supermemory"},
            },
        ]

    def list_jobs(self, job_type=None, status=None, lane=None, limit: int = 20):
        self.calls.append(
            {
                "job_type": job_type,
                "status": status,
                "lane": lane,
                "limit": limit,
            }
        )
        jobs = self.jobs
        if job_type:
            jobs = [job for job in jobs if job["job_type"] == job_type]
        if status:
            jobs = [job for job in jobs if job["status"] == status]
        if lane:
            jobs = [job for job in jobs if job["lane"] == lane]
        return jobs[:limit]

    def get_job(self, job_id: str):
        for job in self.jobs:
            if job["id"] == job_id:
                return job
        return None


@pytest.mark.asyncio
async def test_tasks_list_jobs_supports_lane_filter() -> None:
    app = create_app()
    fake_queue = _FakeQueue()
    app.dependency_overrides[dependencies.get_queue] = lambda: fake_queue

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/tasks?lane=slow")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["jobs"][0]["id"] == "job-slow"
    assert fake_queue.calls == [
        {
            "job_type": None,
            "status": None,
            "lane": "slow",
            "limit": 20,
        }
    ]


@pytest.mark.asyncio
async def test_tasks_list_jobs_rejects_unknown_lane() -> None:
    app = create_app()
    app.dependency_overrides[dependencies.get_queue] = lambda: _FakeQueue()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/tasks?lane=bulk")

    assert response.status_code == 400
    assert "Invalid lane 'bulk'" in response.text
