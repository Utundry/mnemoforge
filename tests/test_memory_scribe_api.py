import pytest

from app.services.job_queue import JobQueue


@pytest.mark.asyncio
async def test_memory_scribe_compact_endpoint_queues_review_only_job(client, monkeypatch):
    submitted: list[tuple[str, dict]] = []

    async def fake_submit(self, job_type: str, payload: dict) -> str:
        submitted.append((job_type, payload))
        return "job-scribe-1"

    monkeypatch.setattr(JobQueue, "submit", fake_submit)

    resp = await client.post(
        "/api/v1/tasks/memory-scribe/compact",
        json={
            "project": "alpha",
            "task_id": "task-1",
            "stage": "handoff",
            "status": "active",
            "raw_notes": "Summary: captured handoff notes\nVerification: unit test",
            "use_llm": False,
        },
    )

    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["job_id"] == "job-scribe-1"
    assert data["job_type"] == "memory_scribe_compact"
    assert data["mutates_memory"] is False
    assert submitted[0][0] == "memory_scribe_compact"
    assert submitted[0][1]["project"] == "alpha"
    assert submitted[0][1]["use_llm"] is False
