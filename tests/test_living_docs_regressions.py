from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient

from app import dependencies
from app.config import settings
from app.main import _enqueue_startup_docs_refresh, create_app
from app.models.docs import DocsSection, DocsStatus
from app.services import docs_service
from app.services.docs_cache_store import get_docs_cache_store
from app.services.ollama_service import OllamaService
from app.services.qdrant_service import QdrantService


def test_docs_cache_path_preserves_safe_project_id() -> None:
    path = docs_service._cache_path("my-proj_1")
    assert path.parent == docs_service._CACHE_DIR
    assert path.name == "my-proj_1.json"


def test_docs_cache_path_sanitizes_unsafe_project_id() -> None:
    path = docs_service._cache_path("../evil")
    assert path.parent == docs_service._CACHE_DIR
    assert path.suffix == ".json"
    assert path.name.startswith("project-")
    assert "/" not in path.name
    assert "\\" not in path.name


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.jobs: list[dict] = []

    async def submit(self, job_type: str, payload: dict) -> str:
        self.calls.append((job_type, payload))
        job = {
            "id": "job-test",
            "job_type": job_type,
            "status": "queued",
            "payload": payload,
        }
        self.jobs.append(job)
        return "job-test"

    def list_jobs(self, job_type: str | None = None, status: str | None = None, limit: int = 20):
        jobs = self.jobs
        if job_type:
            jobs = [job for job in jobs if job.get("job_type") == job_type]
        if status:
            jobs = [job for job in jobs if job.get("status") == status]
        return jobs[:limit]


async def _build_client_with_queue(fake_queue: _FakeQueue) -> AsyncClient:
    qdrant_client = AsyncQdrantClient(":memory:")
    qdrant_svc = QdrantService(qdrant_client)
    await qdrant_svc.ensure_collection()
    dependencies.set_qdrant_client(qdrant_client)

    ollama = OllamaService.__new__(OllamaService)
    ollama.base_url = "http://mocked"
    ollama.model = "nomic-embed-text"
    ollama.embed = AsyncMock(return_value=[0.1] * settings.embedding_dimensions)
    ollama.embed_batch = AsyncMock(return_value=[[0.1] * settings.embedding_dimensions])
    ollama.generate = AsyncMock(return_value="")
    ollama.health = AsyncMock(return_value=True)
    ollama.close = AsyncMock()
    dependencies.set_ollama_service(ollama)

    app = create_app()
    app.dependency_overrides[dependencies.get_queue] = lambda: fake_queue
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    setattr(client, "_qdrant_client", qdrant_client)
    return client


@pytest_asyncio.fixture
async def client_with_fake_queue():
    settings.qdrant_in_memory = True
    settings.qdrant_collection_name = "test_memories"
    settings.embedding_dimensions = 1024

    fake_queue = _FakeQueue()
    client = await _build_client_with_queue(fake_queue)
    async with client:
        yield client, fake_queue

    qdrant_client = getattr(client, "_qdrant_client", None)
    if qdrant_client is not None:
        await qdrant_client.close()


@pytest.mark.asyncio
async def test_resolve_improvement_rebuilds_docs_for_correct_project(
    client_with_fake_queue, monkeypatch
) -> None:
    client, fake_queue = client_with_fake_queue

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Test improvement",
            "description": "Desc",
            "project": "proj-x",
            "agent_id": "tester",
            "importance_score": 0.5,
            "tags": ["t"],
        },
    )
    assert create.status_code == 201, create.text
    improvement_id = create.json()["id"]
    artifact_key = f"improvement:proj-x:{improvement_id}"

    resolve = await client.post(
        f"/api/v1/artifacts/{artifact_key}/resolve",
        json={
            "acted_by": "owner",
            "action_source": "dashboard_review",
            "reason": "Implemented and verified",
        },
    )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["status"] == "done"
    assert body["type"] == "improvement"
    assert body["project"] == "proj-x"

    fetched = await client.get(f"/api/v1/artifacts/{artifact_key}")
    assert fetched.status_code == 200, fetched.text
    record = fetched.json()
    assert record["status"] == "done"
    assert record["type"] == "improvement"
    assert record["project"] == "proj-x"
    assert record["resolved_at"] is not None

    assert ("task_memoir", {"task_id": improvement_id, "project": "proj-x"}) in fake_queue.calls
    assert ("docs_rebuild", {"project": "proj-x"}) in fake_queue.calls


@pytest.mark.asyncio
async def test_startup_docs_refresh_queues_slow_lane_job() -> None:
    fake_queue = _FakeQueue()

    job_id, queued = await _enqueue_startup_docs_refresh(fake_queue, "mnemoforge", force=False)

    assert queued is True
    assert job_id == "job-test"
    assert fake_queue.calls == [
        (
            "docs_rebuild",
            {
                "project": "mnemoforge",
                "force": False,
                "_queue_lane": "slow",
            },
        )
    ]


@pytest.mark.asyncio
async def test_startup_docs_refresh_reuses_existing_pending_job() -> None:
    fake_queue = _FakeQueue()
    fake_queue.jobs.append(
        {
            "id": "job-existing",
            "job_type": "docs_rebuild",
            "status": "queued",
            "payload": {"project": "mnemoforge", "force": False},
        }
    )

    job_id, queued = await _enqueue_startup_docs_refresh(fake_queue, "mnemoforge", force=False)

    assert queued is False
    assert job_id == "job-existing"
    assert fake_queue.calls == []


@pytest.mark.asyncio
async def test_startup_docs_refresh_skips_when_cache_is_recent() -> None:
    fake_queue = _FakeQueue()
    recent_status = DocsStatus(
        project="mnemoforge",
        generated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        sections={"overview": DocsSection(name="Overview", content="Fresh docs")},
    )
    get_docs_cache_store().upsert("mnemoforge", recent_status.model_dump_json())

    job_id, queued = await _enqueue_startup_docs_refresh(
        fake_queue,
        "mnemoforge",
        force=False,
        min_age_seconds=3600,
    )

    assert queued is False
    assert job_id is None
    assert fake_queue.calls == []


@pytest.mark.asyncio
async def test_overview_falls_back_on_generation_leak(monkeypatch) -> None:
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    monkeypatch.setattr(
        "app.services.cloud_llm.cloud_complete",
        AsyncMock(return_value="I need to create a concise summary.\n\n```md\nsummary\n```"),
    )

    rendered = await docs_service._gen_overview(
        improvements=[{"status": "open"}, {"status": "resolved"}],
        components=[{"name": "Context"}],
        runtime_hints=[{"label": "hint"}],
        tasks=[{"title": "task"}],
        project="alpha",
    )
    assert "I need to create" not in rendered
    assert "```" not in rendered
    assert "**alpha** - 2 improvements tracked" in rendered


@pytest.mark.asyncio
async def test_overview_falls_back_on_russian_generation_leak(monkeypatch) -> None:
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    monkeypatch.setattr(
        "app.services.cloud_llm.cloud_complete",
        AsyncMock(return_value="Я создам краткий обзор проекта и сначала перечислю шаги."),
    )

    rendered = await docs_service._gen_overview(
        improvements=[{"status": "open"}],
        components=[{"name": "Context"}],
        runtime_hints=[],
        tasks=[],
        project="alpha",
    )
    assert "Я создам" not in rendered
    assert "сначала" not in rendered.lower()
    assert "**alpha** - 1 improvements tracked" in rendered


@pytest.mark.asyncio
async def test_architecture_falls_back_on_generation_leak(monkeypatch) -> None:
    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: True)
    monkeypatch.setattr(
        "app.services.cloud_llm.cloud_complete",
        AsyncMock(return_value="Let me organize the information about the project.\n\nFirst, let me identify the main components."),
    )

    rendered = await docs_service._gen_architecture(
        [{"name": "Context", "component_id": "context", "purpose": "Builds project context."}],
        "alpha",
    )
    assert "Let me organize" not in rendered
    assert "**Components:**" in rendered
    assert "Context" in rendered
