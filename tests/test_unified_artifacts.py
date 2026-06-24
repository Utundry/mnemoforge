from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4, UUID

from app.models.unified_artifact import (
    ArtifactKey,
    UnifiedArtifactRecord,
    UnifiedArtifactResolveRequest,
    UnifiedArtifactReopenRequest,
    to_unified_status,
    from_unified_status,
)
from app.services.improvements_store import get_improvements_store
from app.services.memory_store import get_memory_store
from app.services import project_identity_service
from app.services.project_tasks_store import get_project_tasks_store
from app.services.unified_artifact_service import UnifiedArtifactService, get_unified_artifact_service


@pytest.mark.asyncio
async def test_artifact_key_parse_valid() -> None:
    """Test parsing valid artifact keys."""
    key = ArtifactKey.parse("improvement:mnemoforge:123e4567-e89b-12d3-a456-426614174000")
    assert key.type == "improvement"
    assert key.project == "mnemoforge"
    assert key.local_id == "123e4567-e89b-12d3-a456-426614174000"

    key = ArtifactKey.parse("task:myproject:987f6543-e21b-43d3-b456-426614174999")
    assert key.type == "task"
    assert key.project == "myproject"
    assert key.local_id == "987f6543-e21b-43d3-b456-426614174999"


@pytest.mark.asyncio
async def test_artifact_key_parse_invalid_format() -> None:
    """Test parsing invalid artifact key formats."""
    with pytest.raises(ValueError, match="Invalid artifact_key format"):
        ArtifactKey.parse("invalid")

    with pytest.raises(ValueError, match="Invalid artifact_key format"):
        ArtifactKey.parse("improvement:mnemoforge")

    # Note: split(":", 2) allows extra parts, so this doesn't raise an error
    # The key will be parsed with local_id = "123:extra"


@pytest.mark.asyncio
async def test_artifact_key_parse_invalid_type() -> None:
    """Test parsing artifact key with invalid type."""
    with pytest.raises(ValueError, match="Invalid artifact type"):
        ArtifactKey.parse("invalid:mnemoforge:123e4567-e89b-12d3-a456-426614174000")


@pytest.mark.asyncio
async def test_artifact_key_to_uuid() -> None:
    """Test converting local_id to UUID."""
    key = ArtifactKey(type="improvement", project="mnemoforge", local_id="123e4567-e89b-12d3-a456-426614174000")
    uuid = key.to_uuid()
    assert isinstance(uuid, UUID)
    assert str(uuid) == "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.asyncio
async def test_get_unified_artifact_service_singleton() -> None:
    """Test that get_unified_artifact_service returns the same instance."""
    service1 = get_unified_artifact_service()
    service2 = get_unified_artifact_service()
    assert service1 is service2


@pytest.mark.asyncio
async def test_get_artifact_improvement() -> None:
    """Test getting an improvement artifact."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()

    # Create an improvement
    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="Test improvement",
        description="Test description",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Get the artifact
    artifact_key = f"improvement:test-project:{improvement_id}"
    artifact = await service.get_artifact(artifact_key)

    assert artifact.artifact_key == artifact_key
    assert artifact.type == "improvement"
    assert artifact.id == improvement_id
    assert artifact.title == "Test improvement"
    assert artifact.description == "Test description"
    assert artifact.project == "test-project"
    assert artifact.agent_id == "test-agent"
    assert artifact.importance_score == 0.8
    assert artifact.tags == ["test"]
    assert artifact.status == "open"  # Default status


@pytest.mark.asyncio
async def test_get_artifact_task() -> None:
    """Test getting a task artifact."""
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()

    # Create a task
    task_id = str(uuid4())
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()

    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Test task",
        description="Test description",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=["test"],
        topic_path="/test/path",
        created_at=now,
        updated_at=now,
    )

    # Get the artifact
    artifact_key = f"task:test-project:{task_id}"
    artifact = await service.get_artifact(artifact_key)

    assert artifact.artifact_key == artifact_key
    assert artifact.type == "task"
    assert artifact.task_id == task_id
    assert artifact.title == "Test task"
    assert artifact.description == "Test description"
    assert artifact.project == "test-project"
    assert artifact.agent_id == "test-agent"
    assert artifact.source == "manual"
    assert artifact.tags == ["test"]
    assert artifact.topic_path == "/test/path"
    assert artifact.status == "active"


@pytest.mark.asyncio
async def test_semantic_candidates_are_rehydrated_from_sqlite_and_stale_candidates_are_dropped() -> None:
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()
    task_id = str(uuid4())
    missing_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()

    tasks_store.upsert_task(
        memory_id=str(uuid4()),
        task_id=task_id,
        project="semantic-project",
        title="Live project reconstruction bundle generator",
        description="Build a reconstruction bundle after source loss.",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    result = await service.list_artifacts(
        project="semantic-project",
        type_="task",
        query="reconstruct a project after source loss",
        semantic_candidates={
            f"task:semantic-project:{task_id}": 0.91,
            f"task:semantic-project:{missing_id}": 0.99,
        },
    )

    assert [item.task_id for item in result.items] == [task_id]
    assert result.search_mode == "semantic"
    assert result.backend_used == "qdrant_candidates_sqlite_authority"
    assert result.candidate_count == 2
    assert result.sqlite_validated_count == 1
    assert "validated from SQLite" in result.items[0].match_reason


@pytest.mark.asyncio
async def test_semantic_raw_memory_candidate_is_rehydrated_from_sqlite() -> None:
    service = UnifiedArtifactService()
    memory_id = str(uuid4())
    await get_memory_store().upsert(
        memory_id,
        "doc_section",
        "Live project reconstruction bundle generator after source loss.",
        {
            "project": "semantic-project",
            "agent_id": "docs",
            "source": "project_tree_doc",
            "topic_path": "recovery/reconstruction",
            "tags": ["source-loss"],
        },
    )

    result = await service.list_artifacts(
        project="semantic-project",
        query="reconstruct a project after source loss",
        semantic_candidates={
            f"project_tree:semantic-project:{memory_id}": 0.88,
        },
    )

    assert result.sqlite_validated_count == 1
    assert result.items[0].type == "project_tree"
    assert result.items[0].artifact_key == f"project_tree:semantic-project:{memory_id}"
    assert result.items[0].source == "project_tree_doc"


@pytest.mark.asyncio
async def test_get_artifact_task_survives_project_rename_without_registered_alias() -> None:
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()
    task_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()

    tasks_store.upsert_task(
        memory_id=str(uuid4()),
        task_id=task_id,
        project="historical-project-name",
        title="Historical task",
        description="The task id remains stable when the project name changes.",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    artifact = await service.get_artifact(f"task:current-project-name:{task_id}")

    assert artifact.task_id == task_id
    assert artifact.project == "current-project-name"
    assert artifact.artifact_key == f"task:current-project-name:{task_id}"


@pytest.mark.asyncio
async def test_get_artifact_task_does_not_cross_projects_when_task_id_is_ambiguous() -> None:
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()
    task_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()

    for project in ("historical-project-a", "historical-project-b"):
        tasks_store.upsert_task(
            memory_id=str(uuid4()),
            task_id=task_id,
            project=project,
            title=f"Task in {project}",
            description="Duplicate task ids must remain project-scoped.",
            agent_id="test-agent",
            status="active",
            source="manual",
            tags=[],
            created_at=now,
            updated_at=now,
        )

    with pytest.raises(ValueError, match="Task not found"):
        await service.get_artifact(f"task:current-project-name:{task_id}")


@pytest.mark.asyncio
async def test_get_artifact_task_does_not_cross_projects_for_non_uuid_task_id() -> None:
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()
    now = datetime.now(timezone.utc).timestamp()

    tasks_store.upsert_task(
        memory_id=str(uuid4()),
        task_id="human-readable-task-id",
        project="historical-project-name",
        title="Historical task",
        description="Only full UUIDs may use the cross-project fallback.",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(ValueError, match="Task not found"):
        await service.get_artifact("task:current-project-name:human-readable-task-id")


@pytest.mark.asyncio
async def test_get_artifact_not_found() -> None:
    """Test getting a non-existent artifact."""
    service = UnifiedArtifactService()
    artifact_key = "improvement:test-project:123e4567-e89b-12d3-a456-426614174000"

    with pytest.raises(ValueError, match="Improvement not found"):
        await service.get_artifact(artifact_key)


@pytest.mark.asyncio
async def test_get_artifact_invalid_type() -> None:
    """Test getting an artifact with invalid type."""
    service = UnifiedArtifactService()
    artifact_key = "invalid:test-project:123e4567-e89b-12d3-a456-426614174000"

    # Note: The error is raised during parsing, not during get_artifact
    with pytest.raises(ValueError, match="Invalid artifact type"):
        await service.get_artifact(artifact_key)


@pytest.mark.asyncio
async def test_list_artifacts_all() -> None:
    """Test listing all artifacts without filters."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    # Create improvements
    imp1_id = uuid4()
    await improvements_store.insert(
        improvement_id=imp1_id,
        title="Improvement 1",
        description="Description 1",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    imp2_id = uuid4()
    await improvements_store.insert(
        improvement_id=imp2_id,
        title="Improvement 2",
        description="Description 2",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.6,
        tags=["test"],
    )

    # Create tasks
    task1_id = str(uuid4())
    memory1_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory1_id,
        task_id=task1_id,
        project="test-project",
        title="Task 1",
        description="Description 1",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    # List all artifacts
    result = await service.list_artifacts(project="test-project")

    assert result.total == 3
    assert len(result.items) == 3
    # Check that we have both improvements and tasks
    types = {item.type for item in result.items}
    assert types == {"improvement", "task"}


@pytest.mark.asyncio
async def test_list_artifacts_filter_by_type() -> None:
    """Test listing artifacts filtered by type."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    # Create improvements
    imp_id = uuid4()
    await improvements_store.insert(
        improvement_id=imp_id,
        title="Improvement 1",
        description="Description 1",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Create tasks
    task_id = str(uuid4())
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Task 1",
        description="Description 1",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    # List only improvements
    result = await service.list_artifacts(project="test-project", type_="improvement")
    assert result.total == 1
    assert result.items[0].type == "improvement"

    # List only tasks
    result = await service.list_artifacts(project="test-project", type_="task")
    assert result.total == 1
    assert result.items[0].type == "task"


@pytest.mark.asyncio
async def test_list_artifacts_filter_by_status() -> None:
    """Test listing artifacts filtered by status."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()

    # Create improvements with different statuses
    imp1_id = uuid4()
    await improvements_store.insert(
        improvement_id=imp1_id,
        title="Open improvement",
        description="Description 1",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    imp2_id = uuid4()
    await improvements_store.insert(
        improvement_id=imp2_id,
        title="Resolved improvement",
        description="Description 2",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.6,
        tags=["test"],
    )

    # Resolve the second improvement
    await improvements_store.resolve(
        imp2_id,
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )

    # List only open artifacts
    result = await service.list_artifacts(project="test-project", status="open")
    assert result.total == 1
    assert result.items[0].title == "Open improvement"

    # List only done artifacts (resolved maps to done in unified status)
    result = await service.list_artifacts(project="test-project", status="done")
    assert result.total == 1
    assert result.items[0].title == "Resolved improvement"


@pytest.mark.asyncio
async def test_list_artifacts_post_filters_misaligned_store_results() -> None:
    """Unified list should still honor final status even if underlying stores leak extra rows."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    imp_id = uuid4()
    await improvements_store.insert(
        improvement_id=imp_id,
        title="Open improvement with done task",
        description="Description 1",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=str(uuid4()),
        task_id=str(uuid4()),
        project="test-project",
        title="Done task",
        description="Description 2",
        agent_id="test-agent",
        status="done",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    open_result = await service.list_artifacts(project="test-project", status="open")
    assert open_result.total == 1
    assert [item.status for item in open_result.items] == ["open"]

    done_result = await service.list_artifacts(project="test-project", status="done")
    assert done_result.total == 1
    assert [item.status for item in done_result.items] == ["done"]


@pytest.mark.asyncio
async def test_list_artifacts_filter_by_updated_interval() -> None:
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()

    older_id = uuid4()
    await improvements_store.insert(
        improvement_id=older_id,
        title="Older improvement",
        description="Description 1",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
        created_at=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc).timestamp(),
    )

    newer_id = uuid4()
    await improvements_store.insert(
        improvement_id=newer_id,
        title="Newer improvement",
        description="Description 2",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
        created_at=datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc).timestamp(),
    )

    result = await service.list_artifacts(
        project="test-project",
        type_="improvement",
        updated_after=datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc),
    )
    assert result.total == 1
    assert result.items[0].title == "Newer improvement"


@pytest.mark.asyncio
async def test_list_artifacts_filter_accepts_naive_datetime_bounds() -> None:
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()

    older_id = uuid4()
    await improvements_store.insert(
        improvement_id=older_id,
        title="Older naive-bound improvement",
        description="Description 1",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
        created_at=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc).timestamp(),
    )

    newer_id = uuid4()
    await improvements_store.insert(
        improvement_id=newer_id,
        title="Newer naive-bound improvement",
        description="Description 2",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
        created_at=datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc).timestamp(),
    )

    result = await service.list_artifacts(
        project="test-project",
        type_="improvement",
        updated_after=datetime(2026, 4, 11, 0, 0),
    )
    assert result.total == 1
    assert result.items[0].title == "Newer naive-bound improvement"


@pytest.mark.asyncio
async def test_list_artifacts_limit() -> None:
    """Test listing artifacts with limit."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()

    # Create multiple improvements
    for i in range(5):
        imp_id = uuid4()
        await improvements_store.insert(
            improvement_id=imp_id,
            title=f"Improvement {i}",
            description=f"Description {i}",
            project="test-project",
            agent_id="test-agent",
            importance_score=0.8,
            tags=["test"],
        )

    # List with limit
    result = await service.list_artifacts(project="test-project", limit=3)
    assert result.total == 3
    assert len(result.items) == 3


@pytest.mark.asyncio
async def test_resolve_artifact_improvement() -> None:
    """Test resolving an improvement artifact."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()

    # Create an improvement
    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="Test improvement",
        description="Test description",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Resolve the artifact
    request = UnifiedArtifactResolveRequest(
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )
    artifact_key = f"improvement:test-project:{improvement_id}"
    resolved = await service.resolve_artifact(artifact_key, request)

    # Note: resolved maps to "done" in unified status
    assert resolved.status == "done"
    assert resolved.resolved_at is not None

    # Verify in store
    row = await improvements_store.get(improvement_id)
    assert row["status"] == "resolved"


@pytest.mark.asyncio
async def test_resolve_artifact_task() -> None:
    """Test resolving a task artifact."""
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()

    # Create a task
    task_id = str(uuid4())
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Test task",
        description="Test description",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    # Resolve the artifact
    request = UnifiedArtifactResolveRequest(
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )
    artifact_key = f"task:test-project:{task_id}"
    resolved = await service.resolve_artifact(artifact_key, request)

    assert resolved.status == "done"

    # Verify in store
    task = tasks_store.get_task_by_task_id(project="test-project", task_id=task_id)
    assert task["status"] == "done"


@pytest.mark.asyncio
async def test_resolve_artifact_with_linked_task() -> None:
    """Test resolving an improvement with a linked task (bidirectional sync)."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    # Create an improvement
    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="Test improvement",
        description="Test description",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Create a linked task
    task_id = str(improvement_id)  # Use same ID for simplicity
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Test improvement",
        description="Test description",
        agent_id="test-agent",
        status="active",
        source="improvement",
        tags=["test"],
        linked_improvement_id=str(improvement_id),
        created_at=now,
        updated_at=now,
    )

    # Link the improvement to the task
    improvements_store.set_node_id(improvement_id, memory_id)

    # Resolve the improvement
    request = UnifiedArtifactResolveRequest(
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )
    artifact_key = f"improvement:test-project:{improvement_id}"
    resolved = await service.resolve_artifact(artifact_key, request)

    # Note: resolved maps to "done" in unified status
    assert resolved.status == "done"
    assert resolved.linked_artifact_key == f"task:test-project:{task_id}"
    assert resolved.linked_status == "done"

    # Verify task was also resolved
    task = tasks_store.get_task_by_task_id(project="test-project", task_id=task_id)
    assert task["status"] == "done"


@pytest.mark.asyncio
async def test_resolve_artifact_with_linked_improvement() -> None:
    """Test resolving a task with a linked improvement (bidirectional sync)."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    # Create an improvement
    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="Test improvement",
        description="Test description",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Create a linked task
    task_id = str(improvement_id)
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Test improvement",
        description="Test description",
        agent_id="test-agent",
        status="active",
        source="improvement",
        tags=["test"],
        linked_improvement_id=str(improvement_id),
        created_at=now,
        updated_at=now,
    )

    # Link the improvement to the task
    improvements_store.set_node_id(improvement_id, memory_id)

    # Resolve the task
    request = UnifiedArtifactResolveRequest(
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )
    artifact_key = f"task:test-project:{task_id}"
    resolved = await service.resolve_artifact(artifact_key, request)

    assert resolved.status == "done"
    # Note: resolved maps to "done" in unified status
    assert resolved.linked_artifact_key == f"improvement:test-project:{improvement_id}"
    assert resolved.linked_status == "done"

    # Verify improvement was also resolved
    imp = await improvements_store.get(improvement_id)
    assert imp["status"] == "resolved"


@pytest.mark.asyncio
async def test_resolve_task_artifact_uses_project_aliases(monkeypatch) -> None:
    """Resolving through a public alias must close the historical stored task row."""
    project_identity_service.close_project_identity_store()
    alias_store = project_identity_service.ProjectIdentityStore(Path(":memory:"))
    monkeypatch.setattr(project_identity_service, "_STORE", alias_store)
    alias_store.upsert_alias(alias="supermemory", project_id="supermemory", reason="historical name")
    alias_store.upsert_alias(alias="sloplesscode", project_id="supermemory", reason="public rename")
    try:
        service = UnifiedArtifactService()
        improvements_store = get_improvements_store()
        tasks_store = get_project_tasks_store()
        task_id = str(uuid4())
        now = datetime.now(timezone.utc).timestamp()

        await improvements_store.insert(
            improvement_id=UUID(task_id),
            title="Alias lifecycle sync",
            description="Historical project rows should close through the public alias.",
            project="supermemory",
            agent_id="test-agent",
            importance_score=0.8,
            tags=["test"],
        )
        tasks_store.upsert_task(
            memory_id=str(uuid4()),
            task_id=task_id,
            project="supermemory",
            title="Alias lifecycle sync",
            description="Historical project rows should close through the public alias.",
            agent_id="test-agent",
            status="active",
            source="improvement",
            tags=["task_status:active", "project:supermemory"],
            linked_improvement_id=task_id,
            created_at=now,
            updated_at=now,
        )

        resolved = await service.resolve_artifact(
            f"task:sloplesscode:{task_id}",
            UnifiedArtifactResolveRequest(
                acted_by="test-user",
                action_source="test",
                reason="Finished via public alias.",
            ),
        )

        assert resolved.status == "done"
        assert resolved.project == "supermemory"
        stored = tasks_store.get_task_by_task_id(project="supermemory", task_id=task_id)
        assert stored["status"] == "done"
        assert "task_status:done" in stored["tags"]
        assert "task_status:active" not in stored["tags"]
        assert (await improvements_store.get(UUID(task_id)))["status"] == "resolved"

        open_tasks = await service.list_artifacts(project="sloplesscode", type_="task", status="open")
        assert all(item.task_id != task_id for item in open_tasks.items)
    finally:
        alias_store.close()
        monkeypatch.setattr(project_identity_service, "_STORE", None)


@pytest.mark.asyncio
async def test_reopen_artifact_improvement() -> None:
    """Test reopening an improvement artifact."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()

    # Create an improvement
    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="Test improvement",
        description="Test description",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Resolve it first
    await improvements_store.resolve(
        improvement_id,
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )

    # Reopen the artifact
    request = UnifiedArtifactReopenRequest(
        project="test-project",
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
        source="manual",
    )
    artifact_key = f"improvement:test-project:{improvement_id}"
    reopened = await service.reopen_artifact(artifact_key, request)

    assert reopened.status == "open"

    # Verify in store
    row = await improvements_store.get(improvement_id)
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_reopen_artifact_task() -> None:
    """Test reopening a task artifact."""
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()

    # Create a done task
    task_id = str(uuid4())
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Test task",
        description="Test description",
        agent_id="test-agent",
        status="done",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    # Reopen the artifact
    request = UnifiedArtifactReopenRequest(
        project="test-project",
        acted_by="test-user",
        reason="Test reason",
        source="manual",
    )
    artifact_key = f"task:test-project:{task_id}"
    reopened = await service.reopen_artifact(artifact_key, request)

    assert reopened.status == "active"

    # Verify in store
    task = tasks_store.get_task_by_task_id(project="test-project", task_id=task_id)
    assert task["status"] == "active"


@pytest.mark.asyncio
async def test_reopen_artifact_with_linked_task() -> None:
    """Test reopening an improvement with a linked task (bidirectional sync)."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    # Create an improvement
    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="Test improvement",
        description="Test description",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Create a linked task
    task_id = str(improvement_id)
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Test improvement",
        description="Test description",
        agent_id="test-agent",
        status="active",
        source="improvement",
        tags=["test"],
        linked_improvement_id=str(improvement_id),
        created_at=now,
        updated_at=now,
    )

    # Link the improvement to the task
    improvements_store.set_node_id(improvement_id, memory_id)

    # Resolve the improvement (which will also resolve the task)
    await improvements_store.resolve(
        improvement_id,
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )

    # Reopen the improvement
    request = UnifiedArtifactReopenRequest(
        project="test-project",
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )
    artifact_key = f"improvement:test-project:{improvement_id}"
    reopened = await service.reopen_artifact(artifact_key, request)

    assert reopened.status == "open"
    assert reopened.linked_artifact_key == f"task:test-project:{task_id}"
    assert reopened.linked_status == "active"

    # Verify task was also reopened
    task = tasks_store.get_task_by_task_id(project="test-project", task_id=task_id)
    assert task["status"] == "active"


@pytest.mark.asyncio
async def test_reopen_artifact_with_linked_improvement() -> None:
    """Test reopening a task with a linked improvement (bidirectional sync)."""
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    # Create an improvement
    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="Test improvement",
        description="Test description",
        project="test-project",
        agent_id="test-agent",
        importance_score=0.8,
        tags=["test"],
    )

    # Create a linked task
    task_id = str(improvement_id)
    memory_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()
    tasks_store.upsert_task(
        memory_id=memory_id,
        task_id=task_id,
        project="test-project",
        title="Test improvement",
        description="Test description",
        agent_id="test-agent",
        status="active",
        source="improvement",
        tags=["test"],
        linked_improvement_id=str(improvement_id),
        created_at=now,
        updated_at=now,
    )

    # Link the improvement to the task
    improvements_store.set_node_id(improvement_id, memory_id)

    # Resolve the improvement (which will also resolve the task)
    await improvements_store.resolve(
        improvement_id,
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
    )

    # Reopen the task
    request = UnifiedArtifactReopenRequest(
        project="test-project",
        acted_by="test-user",
        action_source="test",
        reason="Test reason",
        source="improvement",
    )
    artifact_key = f"task:test-project:{task_id}"
    reopened = await service.reopen_artifact(artifact_key, request)

    assert reopened.status == "active"
    assert reopened.linked_artifact_key == f"improvement:test-project:{improvement_id}"
    assert reopened.linked_status == "open"

    # Verify improvement was also reopened
    imp = await improvements_store.get(improvement_id)
    assert imp["status"] == "open"


@pytest.mark.asyncio
async def test_to_unified_status_improvement() -> None:
    """Test converting improvement status to unified status."""
    assert to_unified_status("improvement", "open") == "open"
    # Note: resolved maps to "done" in unified status
    assert to_unified_status("improvement", "resolved") == "done"
    assert to_unified_status("improvement", "unknown") == "unknown"


@pytest.mark.asyncio
async def test_to_unified_status_task() -> None:
    """Test converting task status to unified status."""
    assert to_unified_status("task", "planning") == "open"
    # Note: active stays as "active" in unified status
    assert to_unified_status("task", "active") == "active"
    assert to_unified_status("task", "done") == "done"
    assert to_unified_status("task", "unknown") == "unknown"


@pytest.mark.asyncio
async def test_from_unified_status_improvement() -> None:
    """Test converting unified status to improvement status."""
    assert from_unified_status("improvement", "open") == "open"
    # Note: "done" maps back to "resolved" for improvements
    assert from_unified_status("improvement", "done") == "resolved"
    assert from_unified_status("improvement", "unknown") == "unknown"


@pytest.mark.asyncio
async def test_from_unified_status_task() -> None:
    """Test converting unified status to task status."""
    # Note: "open" maps to "planning" for tasks (first match in mapping)
    assert from_unified_status("task", "open") == "planning"
    assert from_unified_status("task", "done") == "done"
    # Note: "active" stays as "active" since it's not in the mapping
    assert from_unified_status("task", "active") == "active"
    assert from_unified_status("task", "unknown") == "unknown"


@pytest.mark.asyncio
async def test_open_status_includes_active_tasks_and_prioritizes_high_value_improvements() -> None:
    service = UnifiedArtifactService()
    improvements_store = get_improvements_store()
    tasks_store = get_project_tasks_store()

    improvement_id = uuid4()
    await improvements_store.insert(
        improvement_id=improvement_id,
        title="High priority process improvement",
        description="Important active improvement should compete with tasks in open work.",
        project="open-work-project",
        agent_id="test-agent",
        importance_score=0.95,
        tags=["process"],
        created_at=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc).timestamp(),
    )

    now = datetime(2026, 5, 2, 10, 0, tzinfo=timezone.utc).timestamp()
    active_task_id = str(uuid4())
    tasks_store.upsert_task(
        memory_id=str(uuid4()),
        task_id=active_task_id,
        project="open-work-project",
        title="Recently updated active task",
        description="Active tasks are part of open work.",
        agent_id="test-agent",
        status="active",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now + 60,
    )
    done_task_id = str(uuid4())
    tasks_store.upsert_task(
        memory_id=str(uuid4()),
        task_id=done_task_id,
        project="open-work-project",
        title="Closed task must stay out",
        description="Done work is not open work.",
        agent_id="test-agent",
        status="done",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now + 120,
    )

    result = await service.list_artifacts(project="open-work-project", status="open", limit=5)

    keys = [item.artifact_key for item in result.items]
    assert keys[0] == f"improvement:open-work-project:{improvement_id}"
    assert f"task:open-work-project:{active_task_id}" in keys
    assert f"task:open-work-project:{done_task_id}" not in keys
    assert {item.status for item in result.items} <= {"open", "active"}


@pytest.mark.asyncio
async def test_semantic_open_status_drops_sqlite_done_candidate() -> None:
    service = UnifiedArtifactService()
    tasks_store = get_project_tasks_store()
    task_id = str(uuid4())
    now = datetime.now(timezone.utc).timestamp()

    tasks_store.upsert_task(
        memory_id=str(uuid4()),
        task_id=task_id,
        project="semantic-open-project",
        title="Stale completed semantic candidate",
        description="Qdrant may still point here, but SQLite says the task is done.",
        agent_id="test-agent",
        status="done",
        source="manual",
        tags=[],
        created_at=now,
        updated_at=now,
    )

    result = await service.list_artifacts(
        project="semantic-open-project",
        status="open",
        type_="task",
        query="stale completed semantic candidate",
        semantic_candidates={f"task:semantic-open-project:{task_id}": 0.99},
    )

    assert result.items == []
    assert result.candidate_count == 1
    assert result.sqlite_validated_count == 0
