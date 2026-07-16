from __future__ import annotations

import pytest
import pytest_asyncio

from tests.test_living_docs_regressions import _FakeQueue, _build_client_with_queue


@pytest_asyncio.fixture
async def client_with_unified_artifacts():
    fake_queue = _FakeQueue()
    client = await _build_client_with_queue(fake_queue)
    async with client:
        yield client, fake_queue

    qdrant_client = getattr(client, "_qdrant_client", None)
    if qdrant_client is not None:
        await qdrant_client.close()


@pytest.mark.asyncio
async def test_open_artifacts_lists_orphan_improvement_without_task_bootstrap(
    client_with_unified_artifacts,
    monkeypatch,
) -> None:
    client, _fake_queue = client_with_unified_artifacts

    async def _noop_bootstrap_task_for_improvement(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.routers.improvements._bootstrap_task_for_improvement",
        _noop_bootstrap_task_for_improvement,
    )

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Orphan improvement remains visible",
            "description": "Governed surfaces must show improvements even before task bootstrap.",
            "project": "proj-router",
            "agent_id": "architect",
            "importance_score": 0.8,
            "tags": ["visibility"],
        },
    )
    assert create.status_code == 201, create.text
    improvement_id = create.json()["id"]

    listed = await client.get(
        "/api/v1/artifacts",
        params={"project": "proj-router", "status": "open", "limit": 50},
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(
        item["artifact_key"] == f"improvement:proj-router:{improvement_id}"
        and item["type"] == "improvement"
        and item["status"] == "open"
        for item in items
    )


@pytest.mark.asyncio
async def test_list_artifacts_accepts_status_query_alias(client_with_unified_artifacts) -> None:
    client, _fake_queue = client_with_unified_artifacts

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Router status alias improvement",
            "description": "Should be filtered via status alias.",
            "project": "proj-router",
            "agent_id": "architect",
            "importance_score": 0.8,
            "tags": ["test"],
        },
    )
    assert create.status_code == 201, create.text
    improvement_id = create.json()["id"]

    resolve = await client.patch(
        f"/api/v1/improvements/{improvement_id}/resolve",
        json={
            "acted_by": "owner",
            "action_source": "test",
            "reason": "Resolve to verify filtering.",
        },
    )
    assert resolve.status_code == 200, resolve.text

    open_result = await client.get(
        "/api/v1/artifacts",
        params={"project": "proj-router", "status": "open", "limit": 50},
    )
    assert open_result.status_code == 200, open_result.text
    assert open_result.json()["items"] == []

    done_result = await client.get(
        "/api/v1/artifacts",
        params={"project": "proj-router", "status": "done", "limit": 50},
    )
    assert done_result.status_code == 200, done_result.text
    assert len(done_result.json()["items"]) == 2
    assert {item["status"] for item in done_result.json()["items"]} == {"done"}


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_query(client_with_unified_artifacts) -> None:
    client, _fake_queue = client_with_unified_artifacts

    for title in ("HTTP download export task", "Unrelated cleanup task"):
        create = await client.post(
            "/api/v1/improvements",
            json={
                "title": title,
                "description": "Query filter regression fixture.",
                "project": "proj-router-query",
                "agent_id": "architect",
                "importance_score": 0.8,
                "tags": ["query-filter"],
            },
        )
        assert create.status_code == 201, create.text

    listed = await client.get(
        "/api/v1/artifacts",
        params={"project": "proj-router-query", "query": "HTTP download", "limit": 50},
    )
    assert listed.status_code == 200, listed.text
    titles = [item["title"] for item in listed.json()["items"]]
    assert any("HTTP download" in title for title in titles)
    assert all("Unrelated cleanup" not in title for title in titles)


@pytest.mark.asyncio
async def test_list_artifacts_uses_topic_aliases_and_ranking(client_with_unified_artifacts) -> None:
    client, _fake_queue = client_with_unified_artifacts

    subject = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Scripted portable data export and import-preview workflows",
            "description": "Cross-platform project data portability with backup restore packages.",
            "project": "proj-router-alias",
            "agent_id": "architect",
            "importance_score": 0.9,
            "tags": ["#data-portability", "#import-preview"],
        },
    )
    assert subject.status_code == 201, subject.text

    diagnostic = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Explain artifact lookup matches and prevent weak-model search spirals",
            "description": (
                "Diagnostic task for an HTTP download search bug: explain artifact lookup "
                "matches so weak agents do not spiral while searching for tasks."
            ),
            "project": "proj-router-alias",
            "agent_id": "architect",
            "importance_score": 0.8,
            "tags": ["#diagnostic-task", "#routing-regression", "#topic-tag-lookup"],
        },
    )
    assert diagnostic.status_code == 201, diagnostic.text

    listed = await client.get(
        "/api/v1/artifacts",
        params={"project": "proj-router-alias", "query": "HTTP download", "type": "task", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items
    assert items[0]["title"] == "Scripted portable data export and import-preview workflows"
    assert items[0]["status"] == "open"
    assert "#http-download" in items[0]["matched_topic_tags"]
    assert "Matched topic aliases" in items[0]["match_reason"]


@pytest.mark.asyncio
async def test_list_artifacts_semantic_mode_reports_qdrant_candidates_and_sqlite_validation(
    client_with_unified_artifacts,
) -> None:
    client, _fake_queue = client_with_unified_artifacts

    created = await client.post(
        "/api/v1/project/tasks",
        json={
            "project": "proj-semantic",
            "title": "Live project reconstruction bundle generator",
            "description": "Create a bundle that lets a fresh agent rebuild work after source loss.",
            "agent_id": "architect",
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    listed = await client.get(
        "/api/v1/artifacts",
        params={
            "project": "proj-semantic",
            "query": "recover project knowledge after losing source code",
            "search_mode": "semantic",
            "type": "task",
            "limit": 10,
        },
    )

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["search_mode"] == "semantic"
    assert body["backend_used"] == "qdrant_candidates_sqlite_authority"
    assert body["candidate_count"] >= 1
    assert body["sqlite_validated_count"] >= 1
    assert any(item["task_id"] == task_id for item in body["items"])


@pytest.mark.asyncio
async def test_resolve_artifact_survives_best_effort_followup_failure(
    client_with_unified_artifacts,
    monkeypatch,
) -> None:
    client, fake_queue = client_with_unified_artifacts

    create = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Router follow-up failure improvement",
            "description": "Should still resolve when follow-up work fails.",
            "project": "proj-router",
            "agent_id": "architect",
            "importance_score": 0.8,
            "tags": ["test"],
        },
    )
    assert create.status_code == 201, create.text
    improvement_id = create.json()["id"]

    async def _noop_ensure_task_for_improvement(*args, **kwargs):
        return None

    async def _boom_record_improvement_task_change(*args, **kwargs):
        raise RuntimeError("simulated follow-up failure")

    monkeypatch.setattr(
        "app.services.project_task_service.ensure_task_for_improvement",
        _noop_ensure_task_for_improvement,
    )
    monkeypatch.setattr(
        "app.services.project_task_service.record_improvement_task_change",
        _boom_record_improvement_task_change,
    )

    resolve = await client.post(
        f"/api/v1/artifacts/improvement:proj-router:{improvement_id}/resolve",
        json={
            "acted_by": "owner",
            "action_source": "test",
            "reason": "Resolve despite follow-up failure.",
        },
    )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["status"] == "done"

    fetched = await client.get(f"/api/v1/artifacts/improvement:proj-router:{improvement_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "done"

    assert ("task_memoir", {"task_id": improvement_id, "project": "proj-router"}) in fake_queue.calls
    assert ("docs_rebuild", {"project": "proj-router"}) in fake_queue.calls
    assert ("rebuild_project_tasks", {"project": "proj-router", "_queue_lane": "slow"}) in fake_queue.calls


@pytest.mark.asyncio
async def test_reconcile_completed_checkpoints_reports_without_closing_by_default(client_with_unified_artifacts) -> None:
    client, _fake_queue = client_with_unified_artifacts

    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "checkpoint-tail-1",
            "project": "proj-router",
            "title": "Close stale task tail",
            "description": "A completed checkpoint exists but task status stayed active.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/checkpoint-tail-1/changes",
        json={
            "project": "proj-router",
            "change_type": "note",
            "content": "\n".join(
                [
                    "[task_checkpoint]",
                    "Checkpoint stage: completed",
                    "Checkpoint status: done",
                    "Summary: Implementation and verification are complete.",
                    "Verification: pytest passed",
                ]
            ),
            "why": "Final checkpoint recorded.",
            "agent_id": "architect",
            "tags": ["task_checkpoint", "task_stage:completed", "task_status:done"],
        },
    )
    assert change.status_code == 201, change.text

    report = await client.post(
        "/api/v1/artifacts/reconcile-completed-checkpoints",
        json={"project": "proj-router"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["scanned_tasks"] >= 1
    assert body["closed_artifact_keys"] == []
    assert body["candidates"][0]["task_artifact_key"] == "task:proj-router:checkpoint-tail-1"
    assert body["candidates"][0]["closure_eligible"] is True
    assert body["candidates"][0]["recommendation"] == "close"
    assert body["review_groups"]["eligible_to_close"] == ["task:proj-router:checkpoint-tail-1"]

    fetched = await client.get("/api/v1/artifacts/task:proj-router:checkpoint-tail-1")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "active"


@pytest.mark.asyncio
async def test_completed_but_open_anomaly_list_is_read_only_and_compact(client_with_unified_artifacts) -> None:
    client, _fake_queue = client_with_unified_artifacts

    for task_id, checkpoint_lines in (
        (
            "anomaly-safe-close",
            [
                "[task_checkpoint]",
                "Checkpoint stage: completed",
                "Checkpoint status: done",
                "Summary: Fully verified.",
            ],
        ),
        ("ordinary-open-task", []),
        (
            "anomaly-needs-review",
            [
                "[task_checkpoint]",
                "Checkpoint stage: completed",
                "Checkpoint status: done",
                "Summary: Done, but follow-up needs review.",
                "Next step: Inspect the follow-up scope.",
            ],
        ),
    ):
        create = await client.post(
            "/api/v1/project/tasks",
            json={
                "task_id": task_id,
                "project": "proj-router",
                "title": f"Task {task_id}",
                "description": "Lifecycle anomaly fixture.",
                "agent_id": "architect",
                "status": "active",
            },
        )
        assert create.status_code == 201, create.text
        if checkpoint_lines:
            change = await client.post(
                f"/api/v1/project/tasks/{task_id}/changes",
                json={
                    "project": "proj-router",
                    "change_type": "note",
                    "content": "\n".join(checkpoint_lines),
                    "why": "Final checkpoint recorded.",
                    "agent_id": "architect",
                    "tags": ["task_checkpoint", "task_stage:completed", "task_status:done"],
                },
            )
            assert change.status_code == 201, change.text

    report = await client.post(
        "/api/v1/artifacts/lifecycle-anomalies/completed-but-open",
        json={"project": "proj-router"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    by_task = {item["task_id"]: item for item in body["candidates"]}
    assert set(by_task) == {"anomaly-safe-close", "anomaly-needs-review"}
    assert by_task["anomaly-safe-close"]["safe_auto_repair"] is True
    assert by_task["anomaly-safe-close"]["recommended_repair"] == "close_as_completed"
    assert by_task["anomaly-safe-close"]["recommended_close_status"] == "completed"
    assert by_task["anomaly-needs-review"]["safe_auto_repair"] is False
    assert by_task["anomaly-needs-review"]["recommended_repair"] == "review_next_step_scope"
    assert body["safe_candidates"] == ["task:proj-router:anomaly-safe-close"]
    assert body["needs_operator_review"] == ["task:proj-router:anomaly-needs-review"]
    assert body["safe_auto_repair_count"] == 1
    assert body["review_required_count"] == 1

    fetched = await client.get("/api/v1/artifacts/task:proj-router:anomaly-safe-close")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "active"


async def test_reconcile_completed_checkpoints_closes_only_strictly_eligible_tasks(client_with_unified_artifacts) -> None:
    client, _fake_queue = client_with_unified_artifacts

    for task_id, next_step in (
        ("checkpoint-close-1", ""),
        ("checkpoint-skip-1", "Run the next implementation slice."),
    ):
        create = await client.post(
            "/api/v1/project/tasks",
            json={
                "task_id": task_id,
                "project": "proj-router",
                "title": f"Task {task_id}",
                "description": "Completed checkpoint reconciliation fixture.",
                "agent_id": "architect",
                "status": "active",
            },
        )
        assert create.status_code == 201, create.text
        lines = [
            "[task_checkpoint]",
            "Checkpoint stage: completed",
            "Checkpoint status: done",
            f"Summary: {task_id} reached a final checkpoint.",
        ]
        if next_step:
            lines.append(f"Next step: {next_step}")
        change = await client.post(
            f"/api/v1/project/tasks/{task_id}/changes",
            json={
                "project": "proj-router",
                "change_type": "note",
                "content": "\n".join(lines),
                "why": "Final checkpoint recorded.",
                "agent_id": "architect",
                "tags": ["task_checkpoint", "task_stage:completed", "task_status:done"],
            },
        )
        assert change.status_code == 201, change.text

    report = await client.post(
        "/api/v1/artifacts/reconcile-completed-checkpoints",
        json={"project": "proj-router", "close": True, "acted_by": "test"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert "task:proj-router:checkpoint-close-1" in body["closed_artifact_keys"]
    assert "task:proj-router:checkpoint-skip-1" in body["skipped_artifact_keys"]

    closed = await client.get("/api/v1/artifacts/task:proj-router:checkpoint-close-1")
    skipped = await client.get("/api/v1/artifacts/task:proj-router:checkpoint-skip-1")
    assert closed.status_code == 200, closed.text
    assert skipped.status_code == 200, skipped.text
    assert closed.json()["status"] == "done"
    assert skipped.json()["status"] == "active"


@pytest.mark.asyncio
async def test_reconcile_completed_checkpoints_distinguishes_follow_up_scope_from_same_artifact_work(
    client_with_unified_artifacts,
) -> None:
    client, _fake_queue = client_with_unified_artifacts

    fixtures = {
        "checkpoint-follow-up-scope": [
            "Next step: Add grouped review in a separate slice.",
            "Next step scope: follow_up_task",
        ],
        "checkpoint-same-artifact-scope": [
            "Next step: Finish the remaining endpoint wiring.",
            "Next step scope: same_artifact_remaining_work",
        ],
        "checkpoint-unknown-scope": [
            "Next step: Inspect the candidates.",
        ],
    }
    for task_id, extra_lines in fixtures.items():
        create = await client.post(
            "/api/v1/project/tasks",
            json={
                "task_id": task_id,
                "project": "proj-router",
                "title": f"Task {task_id}",
                "description": "Completed checkpoint scope fixture.",
                "agent_id": "architect",
                "status": "active",
            },
        )
        assert create.status_code == 201, create.text
        change = await client.post(
            f"/api/v1/project/tasks/{task_id}/changes",
            json={
                "project": "proj-router",
                "change_type": "note",
                "content": "\n".join(
                    [
                        "[task_checkpoint]",
                        "Checkpoint stage: completed",
                        "Checkpoint status: done",
                        f"Summary: {task_id} reached a final checkpoint.",
                        *extra_lines,
                    ]
                ),
                "why": "Final checkpoint recorded.",
                "agent_id": "architect",
                "tags": ["task_checkpoint", "task_stage:completed", "task_status:done"],
            },
        )
        assert change.status_code == 201, change.text

    report = await client.post(
        "/api/v1/artifacts/reconcile-completed-checkpoints",
        json={"project": "proj-router", "close": True, "acted_by": "test"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    by_task = {item["task_id"]: item for item in body["candidates"]}

    assert by_task["checkpoint-follow-up-scope"]["closure_eligible"] is True
    assert by_task["checkpoint-follow-up-scope"]["next_step_scope"] == "follow_up_task"
    assert "task:proj-router:checkpoint-follow-up-scope" in body["closed_artifact_keys"]

    assert by_task["checkpoint-same-artifact-scope"]["closure_eligible"] is False
    assert by_task["checkpoint-same-artifact-scope"]["recommendation"] == "continue_same_artifact"
    assert "task:proj-router:checkpoint-same-artifact-scope" in body["review_groups"]["same_artifact_remaining_work"]

    assert by_task["checkpoint-unknown-scope"]["closure_eligible"] is False
    assert by_task["checkpoint-unknown-scope"]["recommendation"] == "review_next_step_scope_before_closing"
    assert "task:proj-router:checkpoint-unknown-scope" in body["review_groups"]["needs_next_step_scope"]
    suggested = body["suggested_scope_review_batch"]
    assert suggested["project"] == "proj-router"
    suggested_decisions = {item["task_id"]: item for item in suggested["decisions"]}
    assert suggested_decisions["checkpoint-unknown-scope"]["checkpoint_change_id"]
    assert suggested_decisions["checkpoint-unknown-scope"]["next_step_scope"] == "operator_review"
    assert "Current inferred scope: operator_review" in suggested_decisions["checkpoint-unknown-scope"]["reason"]


@pytest.mark.asyncio
async def test_completed_checkpoint_scope_review_makes_legacy_follow_up_closeable(
    client_with_unified_artifacts,
) -> None:
    client, _fake_queue = client_with_unified_artifacts

    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "checkpoint-legacy-scope-review",
            "project": "proj-router",
            "title": "Legacy checkpoint needs scope review",
            "description": "Old checkpoints may have next_step without explicit scope.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text
    change = await client.post(
        "/api/v1/project/tasks/checkpoint-legacy-scope-review/changes",
        json={
            "project": "proj-router",
            "change_type": "note",
            "content": "\n".join(
                [
                    "[task_checkpoint]",
                    "Checkpoint stage: completed",
                    "Checkpoint status: done",
                    "Summary: Legacy checkpoint reached completion.",
                    "Next step: Implement a separate follow-up slice.",
                ]
            ),
            "why": "Final checkpoint recorded.",
            "agent_id": "architect",
            "tags": ["task_checkpoint", "task_stage:completed", "task_status:done"],
        },
    )
    assert change.status_code == 201, change.text
    checkpoint_change_id = change.json()["id"]

    before = await client.post(
        "/api/v1/artifacts/reconcile-completed-checkpoints",
        json={"project": "proj-router"},
    )
    assert before.status_code == 200, before.text
    candidate = next(
        item for item in before.json()["candidates"]
        if item["task_id"] == "checkpoint-legacy-scope-review"
    )
    assert candidate["closure_eligible"] is False
    assert candidate["next_step_scope_source"] == "inferred"

    review = await client.post(
        "/api/v1/artifacts/completed-checkpoint-scope-review",
        json={
            "project": "proj-router",
            "task_id": "checkpoint-legacy-scope-review",
            "checkpoint_change_id": checkpoint_change_id,
            "next_step_scope": "follow_up_task",
            "reason": "The next step is separate follow-up work.",
            "acted_by": "architect",
        },
    )
    assert review.status_code == 200, review.text
    assert review.json()["next_step_scope"] == "follow_up_task"

    close = await client.post(
        "/api/v1/artifacts/reconcile-completed-checkpoints",
        json={"project": "proj-router", "close": True, "acted_by": "architect"},
    )
    assert close.status_code == 200, close.text
    body = close.json()
    reviewed = next(
        item for item in body["candidates"]
        if item["task_id"] == "checkpoint-legacy-scope-review"
    )
    assert reviewed["next_step_scope_source"] == "scope_review"
    assert reviewed["closure_eligible"] is True
    assert "task:proj-router:checkpoint-legacy-scope-review" in body["closed_artifact_keys"]


@pytest.mark.asyncio
async def test_completed_checkpoint_scope_review_batch_records_multiple_decisions(
    client_with_unified_artifacts,
) -> None:
    client, _fake_queue = client_with_unified_artifacts

    checkpoint_ids: dict[str, str] = {}
    for task_id in ("checkpoint-batch-follow-up", "checkpoint-batch-same-artifact"):
        create = await client.post(
            "/api/v1/project/tasks",
            json={
                "task_id": task_id,
                "project": "proj-router",
                "title": f"Task {task_id}",
                "description": "Batch scope review fixture.",
                "agent_id": "architect",
                "status": "active",
            },
        )
        assert create.status_code == 201, create.text
        change = await client.post(
            f"/api/v1/project/tasks/{task_id}/changes",
            json={
                "project": "proj-router",
                "change_type": "note",
                "content": "\n".join(
                    [
                        "[task_checkpoint]",
                        "Checkpoint stage: completed",
                        "Checkpoint status: done",
                        f"Summary: {task_id} reached completion.",
                        "Next step: Review the next slice.",
                    ]
                ),
                "why": "Final checkpoint recorded.",
                "agent_id": "architect",
                "tags": ["task_checkpoint", "task_stage:completed", "task_status:done"],
            },
        )
        assert change.status_code == 201, change.text
        checkpoint_ids[task_id] = change.json()["id"]

    review = await client.post(
        "/api/v1/artifacts/completed-checkpoint-scope-review/batch",
        json={
            "project": "proj-router",
            "acted_by": "architect",
            "decisions": [
                {
                    "task_id": "checkpoint-batch-follow-up",
                    "checkpoint_change_id": checkpoint_ids["checkpoint-batch-follow-up"],
                    "next_step_scope": "follow_up_task",
                    "reason": "Separate follow-up work.",
                },
                {
                    "task_id": "checkpoint-batch-same-artifact",
                    "checkpoint_change_id": checkpoint_ids["checkpoint-batch-same-artifact"],
                    "next_step_scope": "same_artifact_remaining_work",
                    "reason": "The same artifact still needs work.",
                },
                {
                    "task_id": "checkpoint-batch-follow-up",
                    "checkpoint_change_id": checkpoint_ids["checkpoint-batch-follow-up"],
                    "next_step_scope": "follow_up_task",
                },
            ],
        },
    )
    assert review.status_code == 200, review.text
    body = review.json()
    assert body["saved_count"] == 2
    assert body["skipped_count"] == 1
    assert body["error_count"] == 0

    report = await client.post(
        "/api/v1/artifacts/reconcile-completed-checkpoints",
        json={"project": "proj-router"},
    )
    assert report.status_code == 200, report.text
    candidates = {item["task_id"]: item for item in report.json()["candidates"]}
    assert candidates["checkpoint-batch-follow-up"]["closure_eligible"] is True
    assert candidates["checkpoint-batch-follow-up"]["next_step_scope_source"] == "scope_review"
    assert candidates["checkpoint-batch-same-artifact"]["closure_eligible"] is False
    assert candidates["checkpoint-batch-same-artifact"]["recommendation"] == "continue_same_artifact"
