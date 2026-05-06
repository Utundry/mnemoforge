from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.models.docs import DocsSection, DocsStatus


@pytest.mark.asyncio
async def test_docs_section_translate_endpoint_uses_existing_translate_flow(client, tmp_path, monkeypatch):
    import app.services.docs_service as docs_service

    old_cache_dir = docs_service._CACHE_DIR
    docs_service._CACHE_DIR = tmp_path / "docs_cache"
    try:
        status = DocsStatus(
            project="mnemoforge",
            generated_at=datetime.now(timezone.utc),
            sections={
                "overview": DocsSection(name="Overview", content="Original overview in English."),
            },
        )
        docs_service._save_docs_cache("mnemoforge", status)

        with patch(
            "app.services.project_tree_doc.translate_doc",
            new=AsyncMock(return_value="Переведённый overview."),
        ):
            response = await client.get("/api/v1/docs/section/overview/translate?project=mnemoforge")

        assert response.status_code == 200
        body = response.json()
        assert body["section"] == "overview"
        assert body["original"] == "Original overview in English."
        assert body["translated"] == "Переведённый overview."
    finally:
        docs_service._CACHE_DIR = old_cache_dir


@pytest.mark.asyncio
async def test_improvements_report_translate_endpoint_returns_original_and_translated(client):
    create = await client.post("/api/v1/improvements", json={
        "title": "Need better retry policy",
        "description": "Retries should be configurable per component.",
        "project": "mnemoforge",
        "agent_id": "test",
        "importance_score": 0.8,
        "tags": ["reliability"],
    })
    assert create.status_code in {200, 201}

    with patch(
        "app.routers.improvements._generate_report",
        new=AsyncMock(return_value={
            "stats": {"open": 1, "resolved": 0, "total": 1},
            "narrative": "Executive summary in English.\n\n- Priority one",
        }),
    ), patch(
        "app.services.project_tree_doc.translate_doc",
        new=AsyncMock(return_value="Краткая сводка на русском.\n\n- Приоритет один"),
    ):
        response = await client.get("/api/v1/improvements/report/translate?project=mnemoforge")

    assert response.status_code == 200
    body = response.json()
    assert body["project"] == "mnemoforge"
    assert body["original"].startswith("Executive summary in English.")
    assert body["translated"].startswith("Краткая сводка на русском.")


@pytest.mark.asyncio
async def test_docs_section_translate_endpoint_returns_readable_error_detail(client, tmp_path):
    import app.services.docs_service as docs_service

    old_cache_dir = docs_service._CACHE_DIR
    docs_service._CACHE_DIR = tmp_path / "docs_cache"
    try:
        status = DocsStatus(
            project="mnemoforge",
            generated_at=datetime.now(timezone.utc),
            sections={
                "overview": DocsSection(name="Overview", content="Original overview in English."),
            },
        )
        docs_service._save_docs_cache("mnemoforge", status)

        with patch(
            "app.services.project_tree_doc.translate_doc",
            new=AsyncMock(side_effect=RuntimeError("Cloud LLM request timed out")),
        ):
            response = await client.get("/api/v1/docs/section/overview/translate?project=mnemoforge")

        assert response.status_code == 502
        assert response.json()["detail"] == "Cloud LLM request timed out"
    finally:
        docs_service._CACHE_DIR = old_cache_dir
