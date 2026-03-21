from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient

from app import dependencies
from app.config import settings
from app.main import create_app
from app.services import docs_service
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

    async def submit(self, job_type: str, payload: dict) -> str:
        self.calls.append((job_type, payload))
        return "job-test"


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

    invalidated: list[str] = []

    def _fake_invalidate(project: str) -> None:
        invalidated.append(project)

    monkeypatch.setattr(docs_service, "invalidate_docs_cache", _fake_invalidate)

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

    resolve = await client.patch(f"/api/v1/improvements/{improvement_id}/resolve")
    assert resolve.status_code == 200, resolve.text
    assert invalidated == ["proj-x"]

    assert ("task_memoir", {"task_id": improvement_id, "project": "proj-x"}) in fake_queue.calls
    assert ("docs_rebuild", {"project": "proj-x"}) in fake_queue.calls
