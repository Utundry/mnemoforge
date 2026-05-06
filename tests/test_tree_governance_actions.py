from __future__ import annotations

from pathlib import Path

import pytest

from app.routers.improvements import _sync_improvement_to_tree, _sync_resolved_to_tree_node
from app.routers.tree import _sync_node_done_to_improvement
from app.services.improvements_store import get_improvements_store
from app.services.project_tree_store import ProjectTreeStore, get_tree_store


@pytest.fixture(autouse=True)
def _reset_tree_store():
    import app.services.project_tree_store as _pts

    if _pts._store is not None:
        try:
            _pts._store.close()
        except Exception:
            pass
    _pts._store = ProjectTreeStore(Path(":memory:"))
    yield
    if _pts._store is not None:
        try:
            _pts._store.close()
        except Exception:
            pass
        _pts._store = None


@pytest.mark.asyncio
async def test_promote_node_tracks_governance_action(client):
    store = get_tree_store()
    parent_id = store.create_node(title="Project", type="project", status="active", topic_path="alpha")
    node_id = store.create_node(title="Idea", type="idea", status="inbox")

    response = await client.post(
        f"/api/v1/tree/{node_id}/promote",
        json={
            "parent_id": parent_id,
            "acted_by": "owner",
            "action_source": "dashboard_review",
            "reason": "Triaged into active project",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "planning"
    assert body["org_last_action_type"] == "promote_node"
    assert body["org_last_action_by"] == "owner"
    assert body["org_last_action_source"] == "dashboard_review"
    assert body["org_last_action_reason"] == "Triaged into active project"

    node = store.get_node(node_id)
    assert node["parent_id"] == parent_id
    assert node["status"] == "planning"
    assert node["meta_json"]["org_last_action_type"] == "promote_node"
    assert node["meta_json"]["org_last_action_by"] == "owner"
    assert node["meta_json"]["org_last_action_source"] == "dashboard_review"
    assert node["meta_json"]["org_last_action_reason"] == "Triaged into active project"
    assert node["meta_json"]["org_last_action_at"] is not None


@pytest.mark.asyncio
async def test_workspace_actions_track_governance_metadata(client):
    store = get_tree_store()
    project_id = store.create_node(title="Project", type="project", status="active", topic_path="alpha")
    first_workspace = store.register_workspace(project_id=project_id, dir_path="D:/work/alpha-a", canonical=True)
    second_workspace = store.register_workspace(project_id=project_id, dir_path="D:/work/alpha-b", canonical=False)

    promoted = await client.post(
        f"/api/v1/tree/workspaces/{second_workspace}/promote",
        json={
            "acted_by": "owner",
            "action_source": "dashboard_review",
            "reason": "Switch canonical workspace",
        },
    )

    assert promoted.status_code == 200
    promoted_body = promoted.json()
    assert promoted_body["org_last_action_type"] == "promote_workspace"
    assert promoted_body["workspace"]["canonical"] is True
    assert promoted_body["workspace"]["meta_json"]["org_last_action_by"] == "owner"
    assert promoted_body["workspace"]["meta_json"]["org_last_action_source"] == "dashboard_review"
    assert promoted_body["workspace"]["meta_json"]["org_last_action_reason"] == "Switch canonical workspace"

    first = store.get_workspace(first_workspace)
    second = store.get_workspace(second_workspace)
    assert first is not None and first["canonical"] is False
    assert second is not None and second["canonical"] is True
    assert second["promoted_at"] is not None

    archived = await client.post(
        f"/api/v1/tree/workspaces/{first_workspace}/archive",
        json={
            "acted_by": "owner",
            "action_source": "dashboard_review",
            "reason": "Deprecated workspace",
        },
    )

    assert archived.status_code == 200
    archived_body = archived.json()
    assert archived_body["status"] == "archived"
    assert archived_body["org_last_action_type"] == "archive_workspace"
    assert archived_body["workspace"]["meta_json"]["org_last_action_by"] == "owner"
    assert archived_body["workspace"]["meta_json"]["org_last_action_source"] == "dashboard_review"
    assert archived_body["workspace"]["meta_json"]["org_last_action_reason"] == "Deprecated workspace"

    archived_workspace = store.get_workspace(first_workspace)
    assert archived_workspace is not None
    assert archived_workspace["status"] == "archived"
    assert archived_workspace["meta_json"]["org_last_action_type"] == "archive_workspace"
    assert archived_workspace["meta_json"]["org_last_action_at"] is not None


@pytest.mark.asyncio
async def test_upsert_node_by_path_creates_and_updates_structured_knowledge(client):
    response = await client.post(
        "/api/v1/tree/upsert-by-path",
        json={
            "topic_path": "mnemoforge/architecture/mcp/compact-discovery",
            "title": "Compact MCP Discovery",
            "type": "area",
            "status": "active",
            "description": "Compact MCP catalog negotiation and operational tray entrypoint.",
            "responsibility": "Expose a small MCP catalog before the full flat tool list.",
            "source_of_truth": "runtime_contract",
            "runtime_entrypoints": ["initialize", "tools/list", "operational_tray"],
            "tests": ["tests/test_mcp_sse.py"],
            "current_debt": ["Generic MCP clients must opt in."],
            "target_state": "Capable clients negotiate compact discovery at initialize.",
            "projection_targets": ["README.md"],
            "evidence_refs": ["checkpoint:abc"],
            "tags": ["mcp", "compact-discovery"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    node = body["node"]
    assert node["topic_path"] == "mnemoforge/architecture/mcp/compact-discovery"
    assert "structured_knowledge" in node["tags"]
    structured = node["meta_json"]["structured_knowledge"]
    assert structured["responsibility"] == "Expose a small MCP catalog before the full flat tool list."
    assert structured["runtime_entrypoints"] == ["initialize", "tools/list", "operational_tray"]

    updated = await client.post(
        "/api/v1/tree/upsert-by-path",
        json={
            "topic_path": "mnemoforge/architecture/mcp/compact-discovery",
            "title": "Compact MCP Discovery",
            "type": "area",
            "status": "active",
            "target_state": "Compact discovery is the preferred client path.",
            "structured_fields": {"projection_policy": "Markdown is generated or validated from structured nodes."},
            "tags": ["docs-projection"],
        },
    )

    assert updated.status_code == 200, updated.text
    updated_body = updated.json()
    assert updated_body["created"] is False
    updated_node = updated_body["node"]
    updated_structured = updated_node["meta_json"]["structured_knowledge"]
    assert updated_structured["responsibility"] == "Expose a small MCP catalog before the full flat tool list."
    assert updated_structured["target_state"] == "Compact discovery is the preferred client path."
    assert updated_structured["projection_policy"] == "Markdown is generated or validated from structured nodes."
    assert "docs-projection" in updated_node["tags"]


@pytest.mark.asyncio
async def test_archive_node_tracks_governance_action(client):
    store = get_tree_store()
    node_id = store.create_node(title="Task", type="task", status="active", topic_path="alpha/task")

    response = await client.request(
        "DELETE",
        f"/api/v1/tree/{node_id}",
        json={
            "acted_by": "owner",
            "action_source": "dashboard_review",
            "reason": "No longer relevant",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "archived"
    assert body["org_last_action_type"] == "archive_node"
    assert body["org_last_action_by"] == "owner"
    assert body["org_last_action_source"] == "dashboard_review"
    assert body["org_last_action_reason"] == "No longer relevant"

    node = store.get_node(node_id)
    assert node is not None
    assert node["status"] == "archived"
    assert node["meta_json"]["org_last_action_type"] == "archive_node"
    assert node["meta_json"]["org_last_action_by"] == "owner"
    assert node["meta_json"]["org_last_action_source"] == "dashboard_review"
    assert node["meta_json"]["org_last_action_reason"] == "No longer relevant"
    assert node["meta_json"]["org_last_action_at"] is not None


@pytest.mark.asyncio
async def test_tree_done_sync_marks_improvement_resolved_with_system_audit():
    tree_store = get_tree_store()
    improvement_store = get_improvements_store()
    improvement_id, _ = await improvement_store.upsert_by_title(
        title="Finish integration",
        description="Done from tree side",
        project="alpha",
        agent_id="tester",
    )
    node_id = tree_store.create_node(title="Task", type="task", status="done", topic_path="alpha/task")
    tree_store.update_node(node_id, meta_json={"improvement_id": str(improvement_id)})

    await _sync_node_done_to_improvement(node_id, tree_store)

    improvement = await improvement_store.get(improvement_id)
    assert improvement is not None
    assert improvement["status"] == "resolved"
    assert improvement["last_status_action"] == "resolve_improvement"
    assert improvement["last_status_acted_by"] == "system"
    assert improvement["last_status_action_source"] == "linked_tree_completion"
    assert improvement["last_status_action_reason"] == f"Tree node {node_id} marked done"


@pytest.mark.asyncio
async def test_improvement_resolved_sync_marks_node_done_with_system_audit():
    tree_store = get_tree_store()
    node_id = tree_store.create_node(title="Task", type="task", status="active", topic_path="alpha/task")

    await _sync_resolved_to_tree_node(node_id)

    node = tree_store.get_node(node_id)
    assert node is not None
    assert node["status"] == "done"
    assert node["meta_json"]["org_last_action_type"] == "resolve_improvement_sync"
    assert node["meta_json"]["org_last_action_by"] == "system"
    assert node["meta_json"]["org_last_action_source"] == "linked_improvement_resolution"
    assert node["meta_json"]["org_last_action_reason"] == "Linked improvement resolved"
    assert node["meta_json"]["org_last_action_at"] is not None


@pytest.mark.asyncio
async def test_sync_improvement_to_tree_reuses_equivalent_existing_task():
    tree_store = get_tree_store()
    improvement_store = get_improvements_store()
    project_id = tree_store.create_node(title="Alpha", type="project", status="active", topic_path="alpha")
    existing_task_id = tree_store.create_node(
        title="Need better retry policy",
        type="task",
        parent_id=project_id,
        description="Existing task",
        status="planning",
        tags=["ops"],
    )
    improvement_id, _ = await improvement_store.upsert_by_title(
        title="Need better retry policy",
        description="Sync into tree",
        project="alpha",
        agent_id="tester",
        tags=["backend"],
    )

    await _sync_improvement_to_tree(
        improvement_id,
        "alpha",
        "Need better retry policy",
        "Sync into tree",
        ["backend"],
    )

    tasks = [
        node for node in tree_store.get_children(project_id, include_archived=True)
        if node["type"] == "task" and node["title"] == "Need better retry policy"
    ]
    assert len(tasks) == 1
    node = tree_store.get_node(existing_task_id)
    assert node is not None
    assert set(node["tags"]) == {"ops", "backend"}
    assert node["meta_json"]["improvement_id"] == str(improvement_id)
    improvement = await improvement_store.get(improvement_id)
    assert improvement is not None
    assert improvement["node_id"] == existing_task_id


@pytest.mark.asyncio
async def test_project_tree_exact_dedupe_relinks_improvements_and_journal():
    tree_store = get_tree_store()
    improvement_store = get_improvements_store()
    project_id = tree_store.create_node(title="Alpha", type="project", status="active", topic_path="alpha")
    canonical_id = tree_store.create_node(
        title="Bootstrap external project workflow",
        type="task",
        parent_id=project_id,
        description="Canonical task",
        status="planning",
        tags=["bootstrap"],
    )
    duplicate_id = tree_store.create_node(
        title="Bootstrap external project workflow",
        type="task",
        parent_id=project_id,
        description="Duplicate task",
        status="planning",
        tags=["external"],
    )
    tree_store.add_journal_entry(duplicate_id, "Duplicate journal entry", session_id="s1")
    improvement_id, _ = await improvement_store.upsert_by_title(
        title="Bootstrap external project workflow",
        description="Linked to duplicate node",
        project="alpha",
        agent_id="tester",
    )
    improvement_store.set_node_id(improvement_id, duplicate_id)

    result = tree_store.dedupe_exact_nodes(
        limit_groups=10,
        relink_node_reference=improvement_store.replace_node_id,
    )

    assert result["merged_groups"] == 1
    assert result["deleted_nodes"] == 1
    remaining = [
        node for node in tree_store.get_children(project_id, include_archived=True)
        if node["type"] == "task" and node["title"] == "Bootstrap external project workflow"
    ]
    assert len(remaining) == 1
    survivor = remaining[0]
    assert survivor["id"] in {canonical_id, duplicate_id}
    assert set(survivor["tags"]) == {"bootstrap", "external"}
    journal = tree_store.get_journal(survivor["id"])
    assert len(journal) == 1
    assert journal[0]["content"] == "Duplicate journal entry"
    improvement = await improvement_store.get(improvement_id)
    assert improvement is not None
    assert improvement["node_id"] == survivor["id"]
