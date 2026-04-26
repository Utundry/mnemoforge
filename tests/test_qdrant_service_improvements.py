from __future__ import annotations

import pytest

from app.models.memory import ImprovementCreate
from app.services.qdrant_service import QdrantService


@pytest.mark.asyncio
async def test_qdrant_service_resolve_improvement_tracks_status_metadata():
    from qdrant_client import AsyncQdrantClient

    qdrant_client = AsyncQdrantClient(":memory:")
    service = QdrantService(qdrant_client)
    await service.ensure_collection()

    improvement = ImprovementCreate(
        title="Legacy improvement",
        description="Needs legacy service resolve",
        project="alpha",
        agent_id="tester",
        importance_score=0.7,
        tags=["legacy"],
    )
    memory_id = await service.insert_improvement(improvement, [0.1] * 1024)

    project = await service.resolve_improvement(
        memory_id,
        acted_by="owner",
        action_source="dashboard_review",
        reason="Verified fixed",
    )
    assert project == "alpha"

    points = await qdrant_client.retrieve(
        collection_name=service._collection,
        ids=[str(memory_id)],
        with_payload=True,
        with_vectors=False,
    )
    assert points
    payload = points[0].payload or {}
    assert payload["status"] == "resolved"
    assert payload["last_status_action"] == "resolve_improvement"
    assert payload["last_status_acted_by"] == "owner"
    assert payload["last_status_action_source"] == "dashboard_review"
    assert payload["last_status_action_reason"] == "Verified fixed"
    assert payload["last_status_action_at"]

    await qdrant_client.close()
