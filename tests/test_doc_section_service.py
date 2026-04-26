from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from qdrant_client.http import models as qmodels

from app.models.docs import DocsSection, DocsStatus
from app.services.doc_section_service import build_doc_section_memory, list_doc_sections, sync_effective_doc_sections
from app.services import doc_section_service
from app.services.memory_store import get_memory_store


class _FakePoint:
    def __init__(self, point_id: str, payload: dict | None):
        self.id = point_id
        self.payload = payload


class _FakeClient:
    def __init__(self) -> None:
        self.points: dict[str, dict] = {}

    async def upsert(self, collection_name: str, points: list) -> None:
        for point in points:
            self.points[str(point.id)] = dict(point.payload)

    async def scroll(self, collection_name: str, scroll_filter=None, limit: int = 100, with_payload: bool = True, with_vectors: bool = False):
        rows: list[_FakePoint] = []
        for point_id, payload in self.points.items():
            match = True
            for cond in scroll_filter.must if scroll_filter else []:
                expected = getattr(cond.match, "value", None)
                if payload.get(cond.key) != expected:
                    match = False
                    break
            if match:
                rows.append(_FakePoint(point_id, payload if with_payload else None))
        return rows[:limit], None

    async def delete(self, collection_name: str, points_selector) -> None:
        for point_id in points_selector.points:
            self.points.pop(str(point_id), None)


class _FakeOllama:
    async def embed(self, content: str):
        return [0.1, 0.2]


@pytest.mark.asyncio
async def test_sync_effective_doc_sections_upserts_and_lists_rows() -> None:
    fake_client = _FakeClient()
    qdrant = SimpleNamespace(_client=fake_client, _collection="memories")
    status = DocsStatus(
        project="alpha",
        generated_at=datetime.now(timezone.utc),
        sections={
            "overview": DocsSection(name="Overview", content="Alpha overview."),
            "architecture": DocsSection(name="Architecture", content="Alpha architecture."),
        },
        candidate_generated_at=datetime.now(timezone.utc),
        candidate_sections={
            "overview": DocsSection(name="Overview", content="Candidate overview."),
        },
    )

    synced = await sync_effective_doc_sections(qdrant, _FakeOllama(), status)
    rows = await list_doc_sections(fake_client, "memories", "alpha")

    assert len(synced) == 2
    assert [row["meta"]["section_key"] for row in rows] == ["architecture", "overview"]
    assert rows[0]["category"] == "doc_section"
    assert rows[0]["project"] == "alpha"
    assert rows[0]["status"] == "active"
    assert rows[0]["meta"]["entity_type"] == "doc_section"
    assert fake_client.points[synced[0]]["content"].startswith("doc_section_ref:")

    stored = await get_memory_store().get(synced[0])
    assert stored is not None
    assert stored["category"] == "doc_section"
    assert stored["content"].startswith("Alpha ")


def test_build_doc_section_memory_truncates_long_content() -> None:
    long_content = "A" * 12000
    status = DocsStatus(
        project="alpha",
        generated_at=datetime.now(timezone.utc),
        sections={"decisions": DocsSection(name="Decision Log", content=long_content)},
    )

    memory = build_doc_section_memory("alpha", "decisions", status)

    assert memory is not None
    assert len(memory.content) < 10000
    assert memory.meta["truncated_for_memory"] is True


@pytest.mark.asyncio
async def test_backfill_legacy_doc_sections_to_sqlite_and_rewrite_refs(client) -> None:
    from app.dependencies import get_qdrant

    doc_id = str(doc_section_service.doc_section_memory_id("alpha", "overview"))
    content = "Legacy docs section still stored fully in qdrant payload."
    payload = {
        "content": content,
        "agent_id": "system",
        "memory_type": "procedural",
        "category": "doc_section",
        "importance_score": 0.82,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "docs-projection",
        "tags": ["doc_section", "project:alpha", "section:overview"],
        "status": "active",
        "project": "alpha",
        "scope": "project",
        "topic_path": "docs/overview",
        "meta": {
            "entity_type": "doc_section",
            "section_key": "overview",
            "section_name": "Overview",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    await get_qdrant()._client.upsert(
        collection_name="test_memories",
        points=[qmodels.PointStruct(id=doc_id, vector=[0.0] * 1024, payload=payload)],
    )

    report = await doc_section_service.backfill_legacy_doc_sections_to_store(
        get_qdrant()._client,
        "test_memories",
        limit=10,
        rewrite_qdrant_refs=True,
        dry_run=False,
    )
    assert report["legacy_candidates"] == 1
    assert report["copied_to_sqlite"] == 1
    assert report["rewritten_qdrant_refs"] == 1

    stored = await get_memory_store().get(doc_id)
    assert stored is not None
    assert stored["category"] == "doc_section"
    assert stored["content"] == content

    points = await get_qdrant()._client.retrieve(
        collection_name="test_memories",
        ids=[doc_id],
        with_payload=True,
        with_vectors=False,
    )
    assert points
    assert points[0].payload["content"] == f"doc_section_ref:{doc_id}"
