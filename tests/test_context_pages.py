from __future__ import annotations

import time

import pytest

from app.services.context_page_store import ContextPageIntegrityError, get_context_page_store
from app.services.project_tasks_store import get_project_tasks_store


@pytest.mark.asyncio
async def test_context_page_store_requires_single_active_entry_and_supersedes_versions():
    store = get_context_page_store()
    parent_ref = "task:alpha:task-1"

    entry = store.create_page(
        parent_ref=parent_ref,
        project="alpha",
        page_kind="entry",
        page_index=1,
        title="Entry",
        summary="Compact entry",
        content="Page one",
        created_by="tester",
    )

    assert entry["page_ref"] == f"context_page:{entry['page_id']}"
    assert entry["status"] == "active"
    with pytest.raises(ContextPageIntegrityError):
        store.create_page(parent_ref=parent_ref, project="alpha", page_kind="entry", page_index=1, summary="duplicate")

    evidence = store.create_page(
        parent_ref=parent_ref,
        project="alpha",
        page_kind="evidence",
        page_index=2,
        summary="Evidence page",
        content="Evidence details",
    )
    packet = store.entry_packet(parent_ref=parent_ref)
    assert packet is not None
    assert packet["has_more"] is True
    assert packet["next_page_ref"] == evidence["page_ref"]
    assert [page["page_kind"] for page in packet["pages"]] == ["entry", "evidence"]

    updated = store.supersede_page(page_id=evidence["page_id"], summary="Updated evidence")
    old = store.get_page(page_id=evidence["page_id"], include_history=True)
    assert updated["version"] == 2
    assert old["status"] == "superseded"
    assert old["superseded_by_page_id"] == updated["page_id"]
    assert store.get_page(page_id=evidence["page_id"]) is None

    active_index = store.ordinary_indexable_pages(project="alpha")
    assert {page["page_id"] for page in active_index} == {entry["page_id"], updated["page_id"]}


@pytest.mark.asyncio
async def test_context_pages_api_and_mcp_forms_use_sqlite_source_of_truth(client):
    task_id = "task-pages-1"
    get_project_tasks_store().upsert_task(
        memory_id="mem-task-pages-1",
        task_id=task_id,
        project="alpha",
        title="Paged task",
        description="Needs pages",
        agent_id="tester",
        status="planning",
        source="test",
        tags=["project:alpha"],
        created_at=time.time(),
        updated_at=time.time(),
    )
    parent_ref = f"task:alpha:{task_id}"

    create_resp = await client.post(
        "/api/v1/context-pages",
        json={
            "project": "alpha",
            "parent_ref": parent_ref,
            "page_kind": "entry",
            "page_index": 1,
            "title": "Entry",
            "summary": "Compact page",
            "content": "Full entry content",
            "created_by": "tester",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    entry = create_resp.json()
    assert entry["source_of_truth"] == "sqlite"

    evidence_resp = await client.post(
        "/api/v1/context-pages",
        json={
            "project": "alpha",
            "parent_ref": parent_ref,
            "page_kind": "evidence",
            "page_index": 2,
            "summary": "Evidence",
            "content": "Evidence details",
        },
    )
    assert evidence_resp.status_code == 201, evidence_resp.text
    evidence = evidence_resp.json()

    entry_packet = (await client.get(f"/api/v1/context-pages/entry?parent_ref={parent_ref}")).json()
    assert entry_packet["has_more"] is True
    assert entry_packet["next_page_ref"] == evidence["page_ref"]
    assert [item["page_kind"] for item in entry_packet["pages"]] == ["entry", "evidence"]

    update_resp = await client.patch(
        f"/api/v1/context-pages/{evidence['page_id']}",
        json={"summary": "Updated evidence", "content": "Updated details", "updated_by": "tester"},
    )
    assert update_resp.status_code == 200, update_resp.text
    updated = update_resp.json()
    assert updated["version"] == 2

    old_resp = await client.get(f"/api/v1/context-pages/{evidence['page_id']}")
    assert old_resp.status_code == 404
    history_resp = await client.get(f"/api/v1/context-pages/{evidence['page_id']}?include_history=true")
    assert history_resp.status_code == 200
    assert history_resp.json()["status"] == "superseded"

    indexable = (await client.get("/api/v1/context-pages/indexable?project=alpha")).json()
    assert {item["page_ref"] for item in indexable["items"]} == {entry["page_ref"], updated["page_ref"]}
    assert all(item["status"] == "active" for item in indexable["items"])


@pytest.mark.asyncio
async def test_mcp_get_reads_context_page_ref_and_archive_excludes_ordinary_retrieval(monkeypatch):
    from app.routers import mcp_sse

    task_id = "task-pages-mcp"
    get_project_tasks_store().upsert_task(
        memory_id="mem-task-pages-mcp",
        task_id=task_id,
        project="alpha",
        title="MCP paged task",
        description="Needs pages",
        agent_id="tester",
        status="planning",
        source="test",
        created_at=time.time(),
        updated_at=time.time(),
    )
    parent_ref = f"task:alpha:{task_id}"

    async def fake_post(api_base: str, path: str, payload: dict):
        from app.services.context_page_store import get_context_page_store
        if path == "/context-pages":
            return get_context_page_store().create_page(**payload)
        if path.endswith("/archive"):
            page_id = path.split("/")[-2]
            return get_context_page_store().archive_page(page_id=page_id, updated_by=payload.get("updated_by", ""))
        raise AssertionError(path)

    async def fake_patch(api_base: str, path: str, payload: dict | None = None):
        from app.services.context_page_store import get_context_page_store
        page_id = path.split("/")[-1]
        return get_context_page_store().supersede_page(page_id=page_id, **(payload or {}))

    monkeypatch.setattr(mcp_sse, "_post", fake_post)
    monkeypatch.setattr(mcp_sse, "_patch", fake_patch)

    create = await mcp_sse._execute_tool(
        "submit",
        {
            "project": "alpha",
            "state": "planning",
            "form_id": "upsert_context_page",
            "payload": {
                "project": "alpha",
                "parent_ref": parent_ref,
                "page_kind": "entry",
                "page_index": 1,
                "summary": "MCP entry",
                "content": "MCP content",
                "created_by": "tester",
            },
        },
        "http://test/api/v1",
    )
    import json

    created_packet = json.loads(create)
    assert created_packet["receipt"]["status"] == "accepted"
    page_ref = created_packet["receipt"]["page_ref"]
    page_id = created_packet["receipt"]["page_id"]

    read = json.loads(
        await mcp_sse._execute_tool(
            "get",
            {"project": "alpha", "ref": page_ref, "response_format": "context"},
            "http://test/api/v1",
        )
    )
    assert read["kind"] == "context_page"
    assert read["ref"] == page_ref
    assert read["summary"] == "MCP entry"
    assert "receipt" not in read

    archive = json.loads(
        await mcp_sse._execute_tool(
            "submit",
            {
                "project": "alpha",
                "state": "planning",
                "form_id": "archive_context_page",
                "payload": {"project": "alpha", "page_id": page_id, "updated_by": "tester"},
            },
            "http://test/api/v1",
        )
    )
    assert archive["receipt"]["status"] == "accepted"

    missing = json.loads(
        await mcp_sse._execute_tool(
            "get",
            {"project": "alpha", "ref": page_ref, "response_format": "json"},
            "http://test/api/v1",
        )
    )
    assert missing["receipt"]["status"] == "not_found"
