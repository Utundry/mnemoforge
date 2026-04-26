from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.dependencies import get_ollama, get_qdrant
from app.services.memory_store import get_memory_store
from app.services.project_tasks_store import get_project_tasks_store
from app.services.qdrant_rebuild_service import reindex_sqlite_backed_qdrant


@pytest.mark.asyncio
async def test_reindex_sqlite_backed_qdrant_restores_supported_surfaces(client) -> None:
    store = get_memory_store()
    task_store = get_project_tasks_store()

    memory_id = "00000000-0000-4000-8000-000000000001"
    handoff_id = "11111111-1111-4111-8111-111111111111"
    memoir_id = "22222222-2222-4222-8222-222222222222"
    doc_id = "33333333-3333-4333-8333-333333333333"
    task_id = "44444444-4444-4444-8444-444444444444"
    change_id = "55555555-5555-4555-8555-555555555555"
    skill_id = "77777777-7777-4777-8777-777777777777"
    code_id = "88888888-8888-4888-8888-888888888888"
    now = datetime.now(timezone.utc)

    await store.upsert(
        memory_id,
        "memory",
        "Canonical memory restored from SQLite.",
        {
            "agent_id": "codex",
            "memory_type": "fact",
            "category": "general",
            "importance_score": 0.6,
            "timestamp": now.isoformat(),
            "source": "conversation",
            "tags": ["project:supermemory", "restore"],
            "status": None,
            "project": "supermemory",
            "scope": "project",
            "meta": {"kind": "note"},
        },
    )
    await store.upsert(
        handoff_id,
        "memory",
        "task: Restore packet\nowner_agent: codex",
        {
            "agent_id": "codex",
            "memory_type": "task",
            "category": "handoff",
            "importance_score": 0.8,
            "timestamp": now.isoformat(),
            "source": "handoff",
            "tags": ["to:codex", "from:claude-code", "handoff_label:restore42"],
            "status": "pending",
            "project": "supermemory",
            "scope": "project",
            "meta": {"owner_agent": "codex", "handoff_label": "restore42"},
        },
    )
    await store.upsert(
        memoir_id,
        "task_memoir",
        "# Memoir: Restore\n\nRecovered from SQLite.",
        {
            "agent_id": "codex",
            "memory_type": "experience",
            "category": "task_memoir",
            "importance_score": 0.7,
            "timestamp": now.isoformat(),
            "source": "memoir:restore",
            "tags": ["memoir", "project:supermemory"],
            "project": "supermemory",
            "meta": {"task_id": "restore", "quality_status": "grounded"},
        },
    )
    await store.upsert(
        skill_id,
        "skill",
        "# Restore Skill\n\nUse this to restore Qdrant.",
        {
            "category": "skill",
            "skill_name": "restore-skill",
            "description": "Restores Qdrant from SQLite.",
            "platform": "claude",
            "domain_tags": ["storage", "recovery"],
            "importance_score": 0.65,
            "agent_id": "codex",
            "timestamp": now.isoformat(),
            "source": "skill-publish:restore-skill",
            "tags": ["restore-skill", "claude", "storage", "recovery"],
            "memory_type": "context",
            "suppressed": False,
            "pinned": True,
        },
    )
    await store.upsert(
        doc_id,
        "doc_section",
        "Overview section rebuilt from SQLite.",
        {
            "agent_id": "system",
            "memory_type": "procedural",
            "category": "doc_section",
            "importance_score": 0.82,
            "timestamp": now.isoformat(),
            "source": "docs-projection",
            "tags": ["doc_section", "project:supermemory", "section:overview"],
            "status": "active",
            "project": "supermemory",
            "scope": "project",
            "topic_path": "docs/overview",
            "meta": {"section_key": "overview", "section_name": "Overview"},
        },
    )
    await store.upsert(
        code_id,
        "code_component",
        "def restore_index():\n    return 'ok'\n",
        {
            "category": "code_component",
            "agent_id": "code-search",
            "memory_type": "context",
            "importance_score": 0.45,
            "timestamp": now.isoformat(),
            "source": "code-index:app/rebuild.py",
            "tags": ["code", "language:python", "kind:function", "path:app/rebuild.py", "symbol:restore_index"],
            "session_id": None,
            "decay_rate": 0.0,
            "code_path": "app/rebuild.py",
            "code_symbol": "restore_index",
            "code_chunk_type": "function",
            "code_language": "python",
            "code_imports": ["app.services.qdrant_rebuild_service"],
        },
    )
    task_store.upsert_task(
        memory_id=task_id,
        task_id="restore-task",
        project="supermemory",
        title="Restore index",
        description="Rebuild task memories from SQLite state.",
        agent_id="codex",
        status="active",
        source="project_task",
        tags=["project:supermemory", "task_id:restore-task"],
        topic_path="tasks/restore",
        linked_improvement_id=None,
        created_at=now.timestamp(),
        updated_at=now.timestamp(),
    )
    task_store.add_change(
        memory_id=change_id,
        task_id="restore-task",
        project="supermemory",
        change_type="decision",
        content="Prefer SQLite as the canonical store.",
        why="Qdrant should remain rebuildable.",
        agent_id="codex",
        source="project_task_change",
        tags=["project:supermemory", "task_id:restore-task"],
        created_at=now.timestamp(),
    )

    report = await reindex_sqlite_backed_qdrant(
        qdrant=get_qdrant(),
        ollama=get_ollama(),
        targets=["memory", "handoff", "skill", "code_component", "task_memoir", "doc_section", "project_task", "task_change"],
        limit=20,
        dry_run=False,
    )

    assert report["planned_upserts"] == 8
    assert report["upserted"] == 8
    assert report["failed"] == 0

    qdrant_client = get_qdrant()._client
    points = await qdrant_client.retrieve(
        collection_name="test_memories",
        ids=[memory_id, handoff_id, memoir_id, doc_id, task_id, change_id, skill_id, code_id],
        with_payload=True,
        with_vectors=False,
    )
    payload_by_id = {str(point.id): dict(point.payload or {}) for point in points}

    assert payload_by_id[memory_id]["content"] == "Canonical memory restored from SQLite."
    assert payload_by_id[memory_id]["category"] == "general"
    assert payload_by_id[handoff_id]["content"] == f"handoff_ref:{handoff_id}"
    assert payload_by_id[handoff_id]["meta"] == {}
    assert payload_by_id[skill_id]["category"] == "skill"
    assert payload_by_id[skill_id]["skill_name"] == "restore-skill"
    assert payload_by_id[skill_id]["pinned"] is True
    assert payload_by_id[code_id]["category"] == "code_component"
    assert payload_by_id[code_id]["code_path"] == "app/rebuild.py"
    assert payload_by_id[code_id]["code_language"] == "python"
    assert payload_by_id[memoir_id]["content"] == f"memoir_ref:{memoir_id}"
    assert payload_by_id[doc_id]["content"] == f"doc_section_ref:{doc_id}"
    assert payload_by_id[task_id]["category"] == "task"
    assert payload_by_id[task_id]["meta"]["entity_type"] == "project_task"
    assert payload_by_id[change_id]["category"] == "task_change"
    assert payload_by_id[change_id]["meta"]["change_type"] == "decision"


@pytest.mark.asyncio
async def test_admin_qdrant_reindex_dry_run_does_not_mutate_qdrant(client) -> None:
    memoir_id = "66666666-6666-4666-8666-666666666666"
    await get_memory_store().upsert(
        memoir_id,
        "task_memoir",
        "# Memoir: Dry run\n\nPreview only.",
        {
            "agent_id": "codex",
            "memory_type": "experience",
            "category": "task_memoir",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "project": "supermemory",
            "meta": {"task_id": "dry-run"},
        },
    )

    response = await client.post(
        "/api/v1/admin/qdrant/reindex-from-sqlite?limit=10&dry_run=true&targets=task_memoir"
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["planned_upserts"] == 1
    assert report["upserted"] == 0
    assert report["failed"] == 0
    assert report["by_target"]["task_memoir"]["planned_upserts"] == 1

    points = await get_qdrant()._client.retrieve(
        collection_name="test_memories",
        ids=[memoir_id],
        with_payload=True,
        with_vectors=False,
    )
    assert points == []


@pytest.mark.asyncio
async def test_reindex_memory_target_excludes_specialized_categories_even_if_legacy_rows_live_in_memory_store(client) -> None:
    store = get_memory_store()
    now = datetime.now(timezone.utc)
    generic_id = "99999999-9999-4999-8999-999999999991"
    legacy_specialized_id = "99999999-9999-4999-8999-999999999992"

    await store.upsert(
        generic_id,
        "memory",
        "Generic memory row that should rebuild through the memory target.",
        {
            "agent_id": "codex",
            "memory_type": "fact",
            "category": "general",
            "importance_score": 0.55,
            "timestamp": now.isoformat(),
            "source": "conversation",
            "tags": ["project:supermemory"],
            "project": "supermemory",
            "scope": "project",
        },
    )
    await store.upsert(
        legacy_specialized_id,
        "memory",
        "# Memoir: Legacy location\n\nThis should not be rebuilt by the generic memory target.",
        {
            "agent_id": "codex",
            "memory_type": "experience",
            "category": "task_memoir",
            "importance_score": 0.7,
            "timestamp": now.isoformat(),
            "source": "memoir:legacy",
            "tags": ["memoir", "project:supermemory"],
            "project": "supermemory",
            "meta": {"task_id": "legacy"},
        },
    )

    report = await reindex_sqlite_backed_qdrant(
        qdrant=get_qdrant(),
        ollama=get_ollama(),
        targets=["memory"],
        limit=10,
        dry_run=True,
    )

    assert report["planned_upserts"] == 1
    assert report["by_target"]["memory"]["planned_upserts"] == 1
