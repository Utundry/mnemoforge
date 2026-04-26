from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json

import pytest
from qdrant_client.http import models as qmodels

from app.config import settings
from app.services.job_queue import get_job_queue
from app.services.improvements_store import get_improvements_store
from app.services.learning_store import get_learning_store
from app.services import memoir_service
from app.services.memory_store import get_memory_store
from app.services.project_task_service import backfill_tasks_from_improvements, list_project_tasks
from app.services.project_tasks_store import ProjectTasksStore


@pytest.mark.asyncio
async def test_project_tasks_api_roundtrip(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-alpha-1",
            "project": "alpha",
            "title": "Build unified context assembly",
            "description": "Introduce a memory-first retrieval bundle.",
            "agent_id": "architect",
            "tags": ["autodocs", "architecture"],
        },
    )
    assert create.status_code == 201, create.text
    task = create.json()
    assert task["task_id"] == "task-alpha-1"
    assert task["project"] == "alpha"
    assert task["status"] == "planning"

    change = await client.post(
        "/api/v1/project/tasks/task-alpha-1/changes",
        json={
            "change_type": "decision",
            "content": "Keep docs as projection, not source of truth.",
            "why": "Unified retrieval should read from governed knowledge entities.",
            "agent_id": "architect",
            "source": "inline_review",
        },
    )
    assert change.status_code == 201, change.text
    change_body = change.json()
    assert change_body["task_id"] == "task-alpha-1"
    assert change_body["project"] == "alpha"
    assert change_body["change_type"] == "decision"

    fetched = await client.get("/api/v1/project/tasks/task-alpha-1?project=alpha")
    assert fetched.status_code == 200, fetched.text
    task_body = fetched.json()
    assert task_body["task_id"] == "task-alpha-1"
    assert len(task_body["changes"]) == 1
    assert task_body["changes"][0]["change_type"] == "decision"

    listed = await client.get("/api/v1/artifacts?project=alpha&type=task&status=open")
    assert listed.status_code == 200, listed.text
    listing = listed.json()
    assert listing["total"] == 1
    assert listing["items"][0]["task_id"] == "task-alpha-1"


@pytest.mark.asyncio
async def test_project_tasks_api_filters_by_updated_interval(client) -> None:
    first = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-interval-1",
            "project": "alpha",
            "title": "Old task",
            "description": "Created first.",
            "agent_id": "architect",
        },
    )
    assert first.status_code == 201, first.text
    first_updated_at = first.json()["updated_at"]

    second = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-interval-2",
            "project": "alpha",
            "title": "New task",
            "description": "Created second.",
            "agent_id": "architect",
        },
    )
    assert second.status_code == 201, second.text
    second_updated_at = second.json()["updated_at"]

    after = await client.get(
        "/api/v1/artifacts",
        params={"project": "alpha", "type": "task", "updated_after": second_updated_at, "limit": 50},
    )
    assert after.status_code == 200, after.text
    after_body = after.json()
    assert after_body["total"] == 1
    assert after_body["items"][0]["task_id"] == "task-interval-2"

    before = await client.get(
        "/api/v1/artifacts",
        params={"project": "alpha", "type": "task", "updated_before": first_updated_at, "limit": 50},
    )
    assert before.status_code == 200, before.text
    before_body = before.json()
    assert before_body["total"] == 1
    assert before_body["items"][0]["task_id"] == "task-interval-1"


@pytest.mark.asyncio
async def test_project_task_reopen_restores_active_state_and_refreshes_capture(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-reopen-1",
            "project": "alpha",
            "title": "Restore reopened task flow",
            "description": "Closed tasks should be able to return to the working set.",
            "agent_id": "architect",
            "status": "done",
        },
    )
    assert create.status_code == 201, create.text

    reopen = await client.post(
        "/api/v1/project/tasks/task-reopen-1/reopen",
        json={
            "project": "alpha",
            "status": "active",
            "reason": "Regression discovered during runtime",
            "acted_by": "architect",
            "source": "operator_review",
        },
    )
    assert reopen.status_code == 200, reopen.text
    reopened = reopen.json()
    assert reopened["task_id"] == "task-reopen-1"
    assert reopened["status"] == "active"

    fetched = await client.get("/api/v1/project/tasks/task-reopen-1?project=alpha")
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["status"] == "active"
    assert any(change["change_type"] == "status_change" and "Reopened task to active." in change["content"] for change in body["changes"])

    jobs = get_job_queue().list_jobs(job_type="task_capture_refresh", limit=20)
    matching = [
        job for job in jobs
        if (job.get("payload") or {}).get("task_id") == "task-reopen-1"
        and (job.get("payload") or {}).get("project") == "alpha"
        and (job.get("payload") or {}).get("trigger") == "task_reopened:active"
    ]
    assert matching


@pytest.mark.asyncio
async def test_project_task_reopen_resolves_project_when_omitted(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-reopen-auto-project",
            "project": "alpha",
            "title": "Resume without explicit project",
            "description": "The reopen flow should resolve project from task_id when possible.",
            "agent_id": "architect",
            "status": "done",
        },
    )
    assert create.status_code == 201, create.text

    reopen = await client.post(
        "/api/v1/project/tasks/task-reopen-auto-project/reopen",
        json={
            "status": "active",
            "reason": "Resume through MCP",
            "acted_by": "architect",
            "source": "operator_review",
        },
    )
    assert reopen.status_code == 200, reopen.text
    reopened = reopen.json()
    assert reopened["project"] == "alpha"
    assert reopened["status"] == "active"

    fetched = await client.get("/api/v1/project/tasks/task-reopen-auto-project?project=alpha")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "active"


@pytest.mark.asyncio
async def test_task_create_enqueues_auto_capture_refresh_job(client) -> None:
    response = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-auto-capture-1",
            "project": "alpha",
            "title": "Auto-capture framing gaps",
            "description": "Create a task and enqueue cheap capture completion automatically.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert response.status_code == 201, response.text

    jobs = get_job_queue().list_jobs(job_type="task_capture_refresh", limit=20)
    matching = [
        job for job in jobs
        if (job.get("payload") or {}).get("task_id") == "task-auto-capture-1"
        and (job.get("payload") or {}).get("project") == "alpha"
    ]
    assert matching
    latest = matching[0]
    assert latest["payload"]["trigger"] == "task_created"
    assert latest["payload"]["_queue_lane"] == "fast"
    assert latest["payload"]["use_local_generation"] is True


@pytest.mark.asyncio
async def test_task_statement_projection_collects_task_changes_and_deferred_findings(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-statement-1",
            "project": "alpha",
            "title": "Stabilize task framing projection",
            "description": "\n".join(
                [
                    "Build a current-task projection from memory artifacts.",
                    "Assumption: task changes are available in canonical storage.",
                    "Constraint: avoid expensive cloud summarization in the hot path.",
                    "Definition of done: current framing and timeline are queryable.",
                ]
            ),
            "agent_id": "architect",
            "tags": ["memory-first", "projection"],
        },
    )
    assert create.status_code == 201, create.text

    decision = await client.post(
        "/api/v1/project/tasks/task-statement-1/changes",
        json={
            "project": "alpha",
            "change_type": "decision",
            "content": "Keep task statement generation deterministic before adding local SLM summarization.",
            "why": "The first slice should minimize token cost and be easy to verify.",
            "agent_id": "architect",
        },
    )
    assert decision.status_code == 201, decision.text

    implementation = await client.post(
        "/api/v1/project/tasks/task-statement-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added a projection service and task statement endpoint.",
            "why": "Task framing should be reconstructed from durable artifacts instead of chat history.",
            "agent_id": "architect",
        },
    )
    assert implementation.status_code == 201, implementation.text

    deferred = await client.post(
        "/api/v1/project/deferred-findings",
        json={
            "project_id": "alpha",
            "task_id": "task-statement-1",
            "finding": "Need better conflict semantics once assumptions and constraints become first-class records.",
            "suggested_follow_up": "Add promotion and contradiction rules after MVP projection lands.",
            "why_it_matters": "Current framing can drift if contradictory records are accepted silently.",
            "severity": "medium",
            "agent_id": "architect",
            "tags": ["governance"],
        },
    )
    assert deferred.status_code == 200, deferred.text

    response = await client.get("/api/v1/project/tasks/task-statement-1/statement?project=alpha")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["task"]["task_id"] == "task-statement-1"
    assert body["current"]["objective"].startswith("Build a current-task projection")
    assert "task changes are available in canonical storage." in body["current"]["assumptions"]
    assert "avoid expensive cloud summarization in the hot path." in body["current"]["constraints"]
    assert "current framing and timeline are queryable." in body["current"]["definition_of_done"]
    assert any("deterministic" in item for item in body["current"]["chosen_decisions"])
    assert any("conflict semantics" in item for item in body["current"]["deferred_work"])
    assert body["diff"]["changed"] is True
    assert any(item["field"] == "assumption" for item in body["diff"]["framing_evolution"])
    assert any(item["kind"] == "deferred_finding" for item in body["timeline"])
    assert body["quality"]["capture_quality"] in {"partial", "complete"}


@pytest.mark.asyncio
async def test_task_statement_projection_surfaces_framing_evolution_and_unresolved_ambiguities(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-statement-evolution-1",
            "project": "alpha",
            "title": "Make framing evolution explicit",
            "description": "\n".join(
                [
                    "Build a projection that explains how task framing changed over time.",
                    "Assumption: early framing is captured directly in the task body.",
                ]
            ),
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    constraint = await client.post(
        "/api/v1/project/tasks/task-statement-evolution-1/changes",
        json={
            "project": "alpha",
            "change_type": "decision",
            "content": "Constraint: keep framing evolution deterministic and artifact-backed.",
            "why": "The first version should be easy to verify from stored evidence.",
            "agent_id": "architect",
        },
    )
    assert constraint.status_code == 201, constraint.text

    question = await client.post(
        "/api/v1/project/tasks/task-statement-evolution-1/changes",
        json={
            "project": "alpha",
            "change_type": "status_change",
            "content": "Open question: should framing evolution collapse duplicate field updates?",
            "why": "The projection needs a clear policy for repeated artifacts.",
            "agent_id": "architect",
        },
    )
    assert question.status_code == 201, question.text

    response = await client.get("/api/v1/project/tasks/task-statement-evolution-1/statement?project=alpha")
    assert response.status_code == 200, response.text
    body = response.json()

    assert any(
        item["field"] == "assumption" and "early framing" in item["value"]
        for item in body["diff"]["framing_evolution"]
    )
    assert any(
        item["field"] == "constraint" and "artifact-backed" in item["value"]
        for item in body["diff"]["framing_evolution"]
    )
    assert any(
        item["field"] == "open_question" and "collapse duplicate field updates" in item["value"]
        for item in body["diff"]["framing_evolution"]
    )
    assert any(
        "collapse duplicate field updates" in item
        for item in body["diff"]["unresolved_ambiguities"]
    )


@pytest.mark.asyncio
async def test_task_statement_projection_surfaces_next_actions_for_incomplete_review_work(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-statement-actions-1",
            "project": "alpha",
            "title": "Drive next actions from projection",
            "description": "Build a task projection that recommends the next operator step.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    await client.post(
        "/api/v1/project/tasks/task-statement-actions-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added projection plumbing for active task views.",
            "why": "Need a real execution trace before suggesting next review work.",
            "agent_id": "architect",
        },
    )

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["Operators should see the next action directly in the task statement response."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-statement-actions-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text

    statement = await client.get("/api/v1/project/tasks/task-statement-actions-1/statement?project=alpha")
    assert statement.status_code == 200, statement.text
    body = statement.json()

    assert any(
        item["source_kind"] == "capture_review" and "Review" in item["action"]
        for item in body["next_actions"]
    )


@pytest.mark.asyncio
async def test_task_statement_projection_surfaces_ready_next_action_when_complete(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-statement-actions-ready-1",
            "project": "alpha",
            "title": "Drive ready next action from projection",
            "description": "\n".join(
                [
                    "Build a task projection that can mark execution as ready.",
                    "Definition of done: the projection exposes a ready-to-execute view.",
                ]
            ),
            "agent_id": "architect",
            "status": "planning",
        },
    )
    assert create.status_code == 201, create.text

    await client.post(
        "/api/v1/project/tasks/task-statement-actions-ready-1/changes",
        json={
            "project": "alpha",
            "change_type": "decision",
            "content": "Keep next-action generation deterministic and grounded in current task evidence.",
            "why": "Ready state should not depend on opaque model reasoning.",
            "agent_id": "architect",
        },
    )

    statement = await client.get("/api/v1/project/tasks/task-statement-actions-ready-1/statement?project=alpha")
    assert statement.status_code == 200, statement.text
    body = statement.json()

    assert body["quality"]["capture_quality"] == "complete"
    assert body["next_actions"][0]["source_kind"] == "ready"
    assert "Proceed with implementation" in body["next_actions"][0]["action"]


@pytest.mark.asyncio
async def test_task_statement_projection_extracts_labeled_fields_from_bracketed_change_prefixes(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-statement-bracketed-1",
            "project": "alpha",
            "title": "Handle bracketed task change prefixes",
            "description": "Build a robust task statement parser for live task-change content.",
            "agent_id": "architect",
            "status": "planning",
        },
    )
    assert create.status_code == 201, create.text

    decision = await client.post(
        "/api/v1/project/tasks/task-statement-bracketed-1/changes",
        json={
            "project": "alpha",
            "change_type": "decision",
            "content": "Definition of done: the statement endpoint exposes current framing and review metadata.",
            "why": "Live task changes are stored with bracketed type prefixes before the labeled field.",
            "agent_id": "architect",
        },
    )
    assert decision.status_code == 201, decision.text

    response = await client.get("/api/v1/project/tasks/task-statement-bracketed-1/statement?project=alpha")
    assert response.status_code == 200, response.text
    body = response.json()

    assert "the statement endpoint exposes current framing and review metadata." in body["current"]["definition_of_done"]
    assert "definition_of_done" not in body["quality"]["missing_artifacts"]


@pytest.mark.asyncio
async def test_task_capture_candidates_fill_missing_fields_with_local_first_path(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-1",
            "project": "alpha",
            "title": "Discipline task-stage capture",
            "description": "Keep structured task capture cheap and grounded in canonical artifacts.",
            "agent_id": "architect",
            "status": "active",
            "tags": ["memory-first"],
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-capture-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added task statement projection and wired it into the project task API.",
            "why": "Agents need a durable framing view before capture completion can fill gaps.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["Canonical task changes remain available while capture completion runs."],
                "constraint": ["Prefer local generation before escalating to expensive cloud models."],
                "definition_of_done": ["Missing capture fields can be suggested and reviewed per task."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    response = await client.post("/api/v1/project/tasks/task-capture-1/capture-candidates?project=alpha")
    assert response.status_code == 200, response.text
    body = response.json()

    assert "assumption" in body["missing_before"]
    assert "constraint" in body["missing_before"]
    assert "definition_of_done" in body["missing_before"]
    assert body["local_generation_used"] is True
    assert body["persisted_count"] >= 5

    by_kind = {}
    for item in body["candidates"]:
        by_kind.setdefault(item["kind"], []).append(item)

    assert by_kind["assumption"][0]["source"] == "local_slm"
    assert by_kind["constraint"][0]["source"] == "local_slm"
    assert by_kind["definition_of_done"][0]["source"] == "local_slm"
    assert by_kind["result_summary"][0]["source"] == "deterministic"
    assert by_kind["handoff_summary"][0]["source"] == "deterministic"

    rows = await get_learning_store().list_artifacts(
        artifact_type="task_capture_candidate",
        scope="project",
        status="active",
        limit=20,
    )
    matching = [
        row for row in rows
        if "project:alpha" in (row.get("tags") or []) and "task_id:task-capture-1" in (row.get("tags") or [])
    ]
    assert len(matching) == len(body["candidates"])


@pytest.mark.asyncio
async def test_task_capture_candidates_reuse_existing_rows_without_duplicates(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-2",
            "project": "alpha",
            "title": "Reuse task capture candidates",
            "description": "Avoid duplicate capture artifacts when the same task is completed twice.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-capture-2/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added a deterministic result summary source.",
            "why": "Repeated capture completion should not insert duplicates for the same task and kind.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["Task capture suggestions are stored as reviewable project artifacts."],
                "constraint": ["Duplicate capture suggestions should be reused instead of reinserted."],
                "definition_of_done": ["Second pass reuses matching candidate rows for the same task."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    first = await client.post("/api/v1/project/tasks/task-capture-2/capture-candidates?project=alpha")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["persisted_count"] >= 5

    second = await client.post("/api/v1/project/tasks/task-capture-2/capture-candidates?project=alpha")
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["persisted_count"] == 0
    assert second_body["reused_count"] == len(second_body["candidates"])
    assert all(item["reused_existing"] is True for item in second_body["candidates"])

    rows = await get_learning_store().list_artifacts(
        artifact_type="task_capture_candidate",
        scope="project",
        status="active",
        limit=30,
    )
    matching = [
        row for row in rows
        if "project:alpha" in (row.get("tags") or []) and "task_id:task-capture-2" in (row.get("tags") or [])
    ]
    assert len(matching) == len(first_body["candidates"])


@pytest.mark.asyncio
async def test_task_capture_candidates_can_be_listed_and_promoted(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-promote-1",
            "project": "alpha",
            "title": "Promote task capture candidates",
            "description": "Start from a sparse task so capture promotion has something to add.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-capture-promote-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added promotion flow for task capture candidates.",
            "why": "Cheap capture should become canonical task state after review.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["Promotion should move reviewable capture into canonical task memory."],
                "result_summary": "Promotion endpoint landed for cheap task capture drafts.",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-promote-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text
    generated_body = generated.json()
    promoted_candidates = {
        item["artifact_id"]: item["content"]
        for item in generated_body["candidates"]
        if item["kind"] in {"assumption", "result_summary"}
    }
    artifact_ids = list(promoted_candidates)
    assert len(artifact_ids) == 2

    listed = await client.get(
        "/api/v1/project/tasks/task-capture-promote-1/capture-candidates?project=alpha&limit=10"
    )
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert listed_body["found"] >= 2

    promoted = await client.post(
        "/api/v1/project/tasks/task-capture-promote-1/capture-candidates/promote?project=alpha",
        json={
            "artifact_ids": artifact_ids,
            "acted_by": "architect",
            "reason": "reviewed and accepted",
        },
    )
    assert promoted.status_code == 200, promoted.text
    promoted_body = promoted.json()
    assert promoted_body["promoted_count"] == 2
    assert promoted_body["archived_count"] == 2

    task = await client.get("/api/v1/project/tasks/task-capture-promote-1?project=alpha")
    assert task.status_code == 200, task.text
    task_body = task.json()
    assert "Assumption: Promotion should move reviewable capture into canonical task memory." in task_body["description"]
    promoted_result_summary = next(
        content
        for artifact_id, content in promoted_candidates.items()
        if artifact_id in promoted_body["promoted_artifact_ids"]
        and content != "Promotion should move reviewable capture into canonical task memory."
    )
    assert any(
        item["change_type"] == "note" and promoted_result_summary in item["content"]
        for item in task_body["changes"]
    )

    rows = await get_learning_store().list_artifacts(
        artifact_type="task_capture_candidate",
        scope="project",
        limit=20,
    )
    archived_ids = {
        str(row.get("id") or "")
        for row in rows
        if str(row.get("status") or "") == "archived"
    }
    assert set(artifact_ids).issubset(archived_ids)


@pytest.mark.asyncio
async def test_task_capture_persists_specialized_artifact_types(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-specialized-1",
            "project": "alpha",
            "title": "Persist specialized capture artifacts",
            "description": "Keep richer capture kinds in dedicated artifact types.",
            "agent_id": "architect",
            "status": "done",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-capture-specialized-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Updated app/services/task_capture_service.py:120 and tests/test_project_tasks.py:1 to persist richer capture kinds.",
            "why": "Need concrete code links and post-completion risk capture.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    deferred = await client.post(
        "/api/v1/project/deferred-findings",
        json={
            "project_id": "alpha",
            "task_id": "task-capture-specialized-1",
            "finding": "Need follow-up validation for edge-case parsing.",
            "suggested_follow_up": "Add another targeted regression test.",
            "why_it_matters": "The implementation is done but some parsing risk remains.",
            "severity": "medium",
            "agent_id": "architect",
        },
    )
    assert deferred.status_code == 200, deferred.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "verification_result": "Validated the persistence path with targeted regression coverage.",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-specialized-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text
    body = generated.json()

    by_kind = {item["kind"]: item for item in body["candidates"]}
    assert by_kind["code_link"]["artifact_type"] == "code_link"
    assert by_kind["remaining_risk"]["artifact_type"] == "remaining_risk"
    assert by_kind["verification_result"]["artifact_type"] == "task_capture_candidate"

    listed = await client.get(
        "/api/v1/project/tasks/task-capture-specialized-1/capture-candidates?project=alpha&limit=20"
    )
    assert listed.status_code == 200, listed.text
    listed_by_kind = {item["kind"]: item for item in listed.json()["candidates"]}
    assert listed_by_kind["code_link"]["artifact_type"] == "code_link"
    assert listed_by_kind["remaining_risk"]["artifact_type"] == "remaining_risk"


@pytest.mark.asyncio
async def test_promote_specialized_capture_kinds_creates_task_state(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-specialized-promote-1",
            "project": "alpha",
            "title": "Promote specialized capture kinds",
            "description": "Sparse framing with a chosen direction already recorded.",
            "agent_id": "architect",
            "status": "done",
        },
    )
    assert create.status_code == 201, create.text

    decision = await client.post(
        "/api/v1/project/tasks/task-capture-specialized-promote-1/changes",
        json={
            "project": "alpha",
            "change_type": "decision",
            "content": "Use task capture artifacts as governed reviewable state before memoir generation.",
            "why": "Need a stable capture-to-governance path.",
            "agent_id": "architect",
        },
    )
    assert decision.status_code == 201, decision.text

    implementation = await client.post(
        "/api/v1/project/tasks/task-capture-specialized-promote-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Touched app/services/task_capture_service.py:42 to wire richer artifact promotion.",
            "why": "Need a real code link candidate.",
            "agent_id": "architect",
        },
    )
    assert implementation.status_code == 201, implementation.text

    deferred = await client.post(
        "/api/v1/project/deferred-findings",
        json={
            "project_id": "alpha",
            "task_id": "task-capture-specialized-promote-1",
            "finding": "Need one more governance review pass for promotion semantics.",
            "suggested_follow_up": "Review edge cases around accepted risk handling.",
            "why_it_matters": "Done tasks still need explicit remaining-risk capture.",
            "severity": "medium",
            "agent_id": "architect",
        },
    )
    assert deferred.status_code == 200, deferred.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "verification_result": "Validated promotion flow with focused regression coverage.",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-specialized-promote-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text
    generated_body = generated.json()
    artifact_ids = [
        item["artifact_id"]
        for item in generated_body["candidates"]
        if item["kind"] in {"decision_candidate", "code_link", "remaining_risk"}
    ]
    assert len(artifact_ids) == 3

    promoted = await client.post(
        "/api/v1/project/tasks/task-capture-specialized-promote-1/capture-candidates/promote?project=alpha",
        json={
            "artifact_ids": artifact_ids,
            "acted_by": "architect",
            "reason": "accepted specialized capture artifacts",
        },
    )
    assert promoted.status_code == 200, promoted.text
    promoted_body = promoted.json()
    assert promoted_body["promoted_count"] == 3

    task = await client.get("/api/v1/project/tasks/task-capture-specialized-promote-1?project=alpha")
    assert task.status_code == 200, task.text
    task_body = task.json()
    assert "Decision candidate: [decision] Use task capture artifacts as governed reviewable state before memoir generation." in task_body["description"]
    assert any(
        item["change_type"] == "implementation" and "Code link:" in item["content"]
        for item in task_body["changes"]
    )
    assert any(
        item["change_type"] == "note" and "Remaining risk:" in item["content"]
        for item in task_body["changes"]
    )


@pytest.mark.asyncio
async def test_list_tasks_counts_specialized_capture_artifacts(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-list-specialized-1",
            "project": "alpha",
            "title": "Count specialized capture artifacts",
            "description": "Specialized capture artifacts should affect task list completeness.",
            "agent_id": "architect",
            "status": "done",
        },
    )
    assert create.status_code == 201, create.text

    await client.post(
        "/api/v1/project/tasks/task-capture-list-specialized-1/changes",
        json={
            "project": "alpha",
            "change_type": "decision",
            "content": "Use specialized artifact types for richer task capture state.",
            "why": "Need decision traceability after completion.",
            "agent_id": "architect",
        },
    )
    await client.post(
        "/api/v1/project/tasks/task-capture-list-specialized-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Touched app/services/task_capture_service.py:88 to emit specialized artifacts.",
            "why": "Need concrete code-link extraction.",
            "agent_id": "architect",
        },
    )
    await client.post(
        "/api/v1/project/deferred-findings",
        json={
            "project_id": "alpha",
            "task_id": "task-capture-list-specialized-1",
            "finding": "Need follow-up review of the remaining edge-case risk.",
            "suggested_follow_up": "Inspect promotion semantics once more.",
            "why_it_matters": "Done tasks still require explicit remaining-risk capture.",
            "severity": "medium",
            "agent_id": "architect",
        },
    )

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps({"verification_result": "Validated with focused regression coverage."}, ensure_ascii=False)

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-list-specialized-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text

    listed = await client.get("/api/v1/artifacts?project=alpha&type=task")
    assert listed.status_code == 200, listed.text
    items = {item["task_id"]: item for item in listed.json()["items"]}
    assert items["task-capture-list-specialized-1"]["task_capture_pending_count"] >= 3
    assert items["task-capture-list-specialized-1"]["task_statement_incomplete"] is True


@pytest.mark.asyncio
async def test_task_statement_projection_surfaces_capture_review_state(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-review-1",
            "project": "alpha",
            "title": "Expose capture review state",
            "description": "Task statement should show pending and promoted capture drafts.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-capture-review-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added review-state projection for task capture drafts.",
            "why": "Agents should see pending and promoted task framing help in one place.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["Review state should be visible directly in the task statement projection."],
                "constraint": ["Avoid extra cloud synthesis for capture review state."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-review-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text
    generated_body = generated.json()
    promoted_ids = [
        item["artifact_id"]
        for item in generated_body["candidates"]
        if item["kind"] in {"assumption", "result_summary"}
    ]
    assert len(promoted_ids) == 2

    before = await client.get("/api/v1/project/tasks/task-capture-review-1/statement?project=alpha")
    assert before.status_code == 200, before.text
    before_body = before.json()
    assert before_body["capture_review"]["pending_count"] >= 2
    assert before_body["capture_review"]["promoted_count"] == 0
    assert any(
        item["kind"] == "assumption"
        for item in before_body["capture_review"]["pending_candidates"]
    )

    promoted = await client.post(
        "/api/v1/project/tasks/task-capture-review-1/capture-candidates/promote?project=alpha",
        json={
            "artifact_ids": promoted_ids,
            "acted_by": "architect",
            "review_source": "dashboard_review",
            "reason": "accepted into task statement",
        },
    )
    assert promoted.status_code == 200, promoted.text

    after = await client.get("/api/v1/project/tasks/task-capture-review-1/statement?project=alpha")
    assert after.status_code == 200, after.text
    after_body = after.json()
    assert after_body["capture_review"]["promoted_count"] == 2
    assert any(
        item["kind"] == "assumption" and item["status"] == "archived"
        for item in after_body["capture_review"]["promoted_candidates"]
    )
    assert any(
        item["status_updated_by"] == "architect"
        and item["status_update_source"] == "dashboard_review"
        and item["status_update_reason"] == "accepted into task statement"
        and item["last_review_action"] == "set_status:archived"
        for item in after_body["capture_review"]["promoted_candidates"]
    )


@pytest.mark.asyncio
async def test_task_statement_projection_counts_specialized_capture_review_candidates(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-review-specialized-1",
            "project": "alpha",
            "title": "Expose specialized capture review state",
            "description": "Task statement should count specialized capture review artifacts too.",
            "agent_id": "architect",
            "status": "done",
        },
    )
    assert create.status_code == 201, create.text

    await client.post(
        "/api/v1/project/tasks/task-capture-review-specialized-1/changes",
        json={
            "project": "alpha",
            "change_type": "decision",
            "content": "Use specialized artifact kinds for richer task capture review.",
            "why": "Statement review should stay aligned with task-list capture summary.",
            "agent_id": "architect",
        },
    )
    await client.post(
        "/api/v1/project/tasks/task-capture-review-specialized-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Touched app/services/task_capture_service.py:88 to emit specialized artifacts.",
            "why": "Need a concrete code-link candidate.",
            "agent_id": "architect",
        },
    )
    await client.post(
        "/api/v1/project/deferred-findings",
        json={
            "project_id": "alpha",
            "task_id": "task-capture-review-specialized-1",
            "finding": "Need explicit remaining-risk review before fully closing the task.",
            "suggested_follow_up": "Inspect specialized promotion semantics one more time.",
            "why_it_matters": "Done tasks still need visible post-completion risk capture.",
            "severity": "medium",
            "agent_id": "architect",
        },
    )

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps({"verification_result": "Validated with focused regression coverage."}, ensure_ascii=False)

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-review-specialized-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text

    statement = await client.get("/api/v1/project/tasks/task-capture-review-specialized-1/statement?project=alpha")
    assert statement.status_code == 200, statement.text
    body = statement.json()

    assert body["capture_review"]["pending_count"] >= 3
    assert any(
        item["kind"] == "decision_candidate"
        for item in body["capture_review"]["pending_candidates"]
    )
    assert any(
        item["kind"] == "code_link"
        for item in body["capture_review"]["pending_candidates"]
    )
    assert any(
        item["kind"] == "remaining_risk"
        for item in body["capture_review"]["pending_candidates"]
    )


@pytest.mark.asyncio
async def test_task_capture_candidates_can_be_rejected(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-reject-1",
            "project": "alpha",
            "title": "Reject duplicate capture drafts",
            "description": "Task review should be able to discard redundant capture candidates.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    await client.post(
        "/api/v1/project/tasks/task-capture-reject-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added task-level review controls for capture candidates.",
            "why": "Agents need a direct reject path for redundant drafts.",
            "agent_id": "architect",
        },
    )

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["This draft duplicates information already captured elsewhere."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-reject-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text
    generated_body = generated.json()
    artifact_ids = [
        item["artifact_id"]
        for item in generated_body["candidates"]
        if item["kind"] == "assumption"
    ]
    assert len(artifact_ids) == 1

    rejected = await client.post(
        "/api/v1/project/tasks/task-capture-reject-1/capture-candidates/reject?project=alpha",
        json={
            "artifact_ids": artifact_ids,
            "acted_by": "architect",
            "review_source": "dashboard_review",
            "reason": "duplicate draft",
        },
    )
    assert rejected.status_code == 200, rejected.text
    rejected_body = rejected.json()
    assert rejected_body["rejected_count"] == 1
    assert rejected_body["archived_count"] == 1

    statement = await client.get("/api/v1/project/tasks/task-capture-reject-1/statement?project=alpha")
    assert statement.status_code == 200, statement.text
    body = statement.json()
    # After rejecting assumption, deterministic candidates (handoff_summary, result_summary) should still be generated
    # but the rejected assumption should not appear again
    assert body["capture_review"]["pending_count"] == 2
    assert not any(
        item["kind"] == "assumption"
        for item in body["capture_review"]["pending_candidates"]
    ), "Rejected assumption should not appear in pending candidates"
    assert any(
        item["kind"] == "assumption"
        and item["status"] == "archived"
        and item["status_updated_by"] == "architect"
        and item["status_update_source"] == "dashboard_review"
        and item["status_update_reason"] == "duplicate draft"
        and item["last_review_action"] == "set_status:archived"
        for item in body["capture_review"]["promoted_candidates"]
    )


@pytest.mark.asyncio
async def test_list_tasks_surfaces_capture_review_summary(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-capture-list-1",
            "project": "alpha",
            "title": "List task capture review state",
            "description": "Task list should highlight incomplete framing from pending capture drafts.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-capture-list-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added list-level capture review summary.",
            "why": "Agents should see which tasks still need framing review.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["List-level summary should expose pending task capture work."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    generated = await client.post(
        "/api/v1/project/tasks/task-capture-list-1/capture-candidates?project=alpha"
    )
    assert generated.status_code == 200, generated.text
    generated_body = generated.json()

    listed_before = await client.get("/api/v1/artifacts?project=alpha&type=task&status=active")
    assert listed_before.status_code == 200, listed_before.text
    items_before = {item["task_id"]: item for item in listed_before.json()["items"]}
    assert items_before["task-capture-list-1"]["task_capture_pending_count"] >= 1
    assert items_before["task-capture-list-1"]["task_capture_promoted_count"] == 0
    assert items_before["task-capture-list-1"]["task_statement_incomplete"] is True

    assumption_id = next(
        item["artifact_id"] for item in generated_body["candidates"] if item["kind"] == "assumption"
    )
    promoted = await client.post(
        "/api/v1/project/tasks/task-capture-list-1/capture-candidates/promote?project=alpha",
        json={
            "artifact_ids": [assumption_id],
            "acted_by": "architect",
            "reason": "accepted from task list workflow",
        },
    )
    assert promoted.status_code == 200, promoted.text

    listed_after = await client.get("/api/v1/artifacts?project=alpha&type=task&status=active")
    assert listed_after.status_code == 200, listed_after.text
    items_after = {item["task_id"]: item for item in listed_after.json()["items"]}
    assert items_after["task-capture-list-1"]["task_capture_pending_count"] >= 0
    assert items_after["task-capture-list-1"]["task_capture_promoted_count"] >= 1


@pytest.mark.asyncio
async def test_active_task_without_definition_of_done_is_incomplete_without_pending_candidates(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-incomplete-no-dod-1",
            "project": "alpha",
            "title": "Active task without DoD",
            "description": "Implement the next slice without a definition of done yet.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-incomplete-no-dod-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added the first bounded implementation step.",
            "why": "Execution exists, but framing is still incomplete.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    task = await client.get("/api/v1/project/tasks/task-incomplete-no-dod-1?project=alpha")
    assert task.status_code == 200, task.text
    body = task.json()
    assert body["task_capture_pending_count"] == 0
    assert body["task_statement_incomplete"] is True

    statement = await client.get("/api/v1/project/tasks/task-incomplete-no-dod-1/statement?project=alpha")
    assert statement.status_code == 200, statement.text
    statement_body = statement.json()
    assert "definition_of_done" in statement_body["quality"]["missing_artifacts"]
    assert statement_body["quality"]["capture_quality"] == "partial"


@pytest.mark.asyncio
async def test_done_task_without_verification_is_incomplete_without_pending_candidates(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-done-no-verification-1",
            "project": "alpha",
            "title": "Done task without verification",
            "description": "\n".join(
                [
                    "Ship the slice to completion.",
                    "Definition of done: implementation is merged and reviewed.",
                ]
            ),
            "agent_id": "architect",
            "status": "done",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-done-no-verification-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Completed the implementation path.",
            "why": "Task is marked done but no verification was captured.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    task = await client.get("/api/v1/project/tasks/task-done-no-verification-1?project=alpha")
    assert task.status_code == 200, task.text
    body = task.json()
    assert body["task_capture_pending_count"] == 0
    assert body["task_statement_incomplete"] is True

    statement = await client.get("/api/v1/project/tasks/task-done-no-verification-1/statement?project=alpha")
    assert statement.status_code == 200, statement.text
    statement_body = statement.json()
    assert "verification_result" in statement_body["quality"]["missing_artifacts"]


@pytest.mark.asyncio
async def test_task_change_auto_capture_queue_is_deduplicated_while_job_pending(client) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-auto-capture-2",
            "project": "alpha",
            "title": "Avoid queue spam",
            "description": "Repeated task changes should not enqueue duplicate capture refresh jobs while one is pending.",
            "agent_id": "architect",
            "status": "active",
        },
    )
    assert create.status_code == 201, create.text

    jobs_before = get_job_queue().list_jobs(job_type="task_capture_refresh", limit=50)
    baseline = len(
        [
            job for job in jobs_before
            if (job.get("payload") or {}).get("task_id") == "task-auto-capture-2"
            and (job.get("payload") or {}).get("project") == "alpha"
        ]
    )

    first = await client.post(
        "/api/v1/project/tasks/task-auto-capture-2/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added the first bounded implementation change.",
            "why": "Capture should refresh after meaningful task progress.",
            "agent_id": "architect",
        },
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/api/v1/project/tasks/task-auto-capture-2/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Added the second bounded implementation change.",
            "why": "The queue should reuse the pending refresh instead of spawning duplicates.",
            "agent_id": "architect",
        },
    )
    assert second.status_code == 201, second.text

    jobs_after = get_job_queue().list_jobs(job_type="task_capture_refresh", limit=50)
    matching = [
        job for job in jobs_after
        if (job.get("payload") or {}).get("task_id") == "task-auto-capture-2"
        and (job.get("payload") or {}).get("project") == "alpha"
    ]
    assert len(matching) <= baseline + 1


@pytest.mark.asyncio
async def test_generate_memoir_uses_canonical_task_and_task_changes(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-memoir-1",
            "project": "alpha",
            "title": "Redesign autodocumentation",
            "description": "Move from fragmented docs to memory-first retrieval.",
            "agent_id": "architect",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-memoir-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Introduced task and task_change as first-class entities.",
            "why": "Memoirs need a canonical task anchor and structured evolution history.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: False)

    from app.dependencies import get_qdrant

    generated = await memoir_service.generate_memoir(
        "task-memoir-1",
        get_qdrant()._client,
        "test_memories",
    )
    assert "Redesign autodocumentation" in generated
    assert "Introduced task and task_change as first-class entities." in generated


@pytest.mark.asyncio
async def test_generate_and_store_memoir_persists_content_in_sqlite_and_qdrant_ref(client, monkeypatch) -> None:
    create = await client.post(
        "/api/v1/project/tasks",
        json={
            "task_id": "task-memoir-store-1",
            "project": "alpha",
            "title": "Persist memoir through SQLite",
            "description": "Keep memoir content recoverable outside Qdrant payloads.",
            "agent_id": "architect",
        },
    )
    assert create.status_code == 201, create.text

    change = await client.post(
        "/api/v1/project/tasks/task-memoir-store-1/changes",
        json={
            "project": "alpha",
            "change_type": "implementation",
            "content": "Moved memoir content into SQLite-backed durable storage.",
            "why": "Qdrant should stay an index, not the only source of truth.",
            "agent_id": "architect",
        },
    )
    assert change.status_code == 201, change.text

    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: False)

    from app.dependencies import get_ollama, get_qdrant

    memoir_id = await memoir_service.generate_and_store_memoir(
        "task-memoir-store-1",
        get_qdrant()._client,
        "test_memories",
        get_ollama(),
        agent_id="architect",
        project="alpha",
    )
    assert memoir_id is not None

    stored = await get_memory_store().get(str(memoir_id))
    assert stored is not None
    assert stored["category"] == "task_memoir"
    assert "Persist memoir through SQLite" in stored["content"]
    assert stored["metadata"]["meta"]["quality_status"] == "grounded"

    points = await get_qdrant()._client.retrieve(
        collection_name="test_memories",
        ids=[str(memoir_id)],
        with_payload=True,
        with_vectors=False,
    )
    assert points
    payload = points[0].payload or {}
    assert payload["category"] == "task_memoir"
    assert payload["content"] == f"memoir_ref:{memoir_id}"
    assert payload["meta"]["quality_status"] == "grounded"


def test_memoir_quality_status_classifies_grounded_vs_weak() -> None:
    grounded = memoir_service.memoir_quality_status(
        {"content": "Task title"},
        [{"content": "Changed implementation"}],
        "# Memoir: Task title\n\nBuilt the thing.",
    )
    weak = memoir_service.memoir_quality_status(
        None,
        [],
        "## Task\n\nUnknown task\n\n_No changes recorded._",
    )

    assert grounded == "grounded"
    assert weak == "weak"


def test_memoir_generation_preconditions_requires_task_and_changes() -> None:
    ready = memoir_service.memoir_generation_preconditions(
        {"content": "Task title", "status": "done"},
        [{"content": "Changed implementation"}],
    )
    missing = memoir_service.memoir_generation_preconditions(
        None,
        [],
    )

    assert ready["ready"] is True
    assert ready["reasons"] == []
    assert missing["ready"] is False
    assert "missing_task" in missing["reasons"]
    assert "missing_changes" in missing["reasons"]


@pytest.mark.asyncio
async def test_backfill_legacy_memoirs_to_sqlite_and_rewrite_refs(client) -> None:
    from app.dependencies import get_qdrant

    legacy_id = "11111111-1111-4111-8111-111111111111"
    legacy_content = "# Memoir: Legacy task\n\nOriginally stored only in Qdrant."
    payload = {
        "content": legacy_content,
        "agent_id": "architect",
        "memory_type": "experience",
        "category": "task_memoir",
        "importance_score": 0.7,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "memoir:legacy-task-1",
        "tags": ["task_id:legacy-task-1", "memoir", "project:alpha"],
        "project": "alpha",
        "meta": {
            "entity_type": "decision_memoir",
            "task_id": "legacy-task-1",
            "quality_status": "grounded",
        },
    }
    await get_qdrant()._client.upsert(
        collection_name="test_memories",
        points=[
            qmodels.PointStruct(
                id=legacy_id,
                vector=[0.0] * settings.embedding_dimensions,
                payload=payload,
            )
        ],
    )

    report = await memoir_service.backfill_legacy_memoirs_to_store(
        get_qdrant()._client,
        "test_memories",
        limit=10,
        rewrite_qdrant_refs=True,
        dry_run=False,
    )
    assert report["scanned"] >= 1
    assert report["legacy_candidates"] == 1
    assert report["copied_to_sqlite"] == 1
    assert report["rewritten_qdrant_refs"] == 1
    assert report["failed"] == 0

    stored = await get_memory_store().get(legacy_id)
    assert stored is not None
    assert stored["category"] == "task_memoir"
    assert stored["content"] == legacy_content
    assert stored["metadata"]["project"] == "alpha"
    assert stored["metadata"]["meta"]["task_id"] == "legacy-task-1"

    points = await get_qdrant()._client.retrieve(
        collection_name="test_memories",
        ids=[legacy_id],
        with_payload=True,
        with_vectors=False,
    )
    assert points
    assert points[0].payload["content"] == f"memoir_ref:{legacy_id}"

    second = await memoir_service.backfill_legacy_memoirs_to_store(
        get_qdrant()._client,
        "test_memories",
        limit=10,
        rewrite_qdrant_refs=True,
        dry_run=False,
    )
    assert second["already_ref_payload"] == 1
    assert second["copied_to_sqlite"] == 0
    assert second["rewritten_qdrant_refs"] == 0


@pytest.mark.asyncio
async def test_admin_memoir_backfill_dry_run_does_not_mutate_storage(client) -> None:
    from app.dependencies import get_qdrant

    legacy_id = "22222222-2222-4222-8222-222222222222"
    legacy_content = "Dry run should not rewrite this payload."
    await get_qdrant()._client.upsert(
        collection_name="test_memories",
        points=[
            qmodels.PointStruct(
                id=legacy_id,
                vector=[0.0] * settings.embedding_dimensions,
                payload={
                    "content": legacy_content,
                    "agent_id": "architect",
                    "category": "task_memoir",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "project": "alpha",
                    "meta": {"task_id": "legacy-task-dry-run"},
                },
            )
        ],
    )

    response = await client.post(
        "/api/v1/admin/memoirs/backfill?limit=10&dry_run=true&rewrite_qdrant_refs=true"
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["legacy_candidates"] == 1
    assert report["copied_to_sqlite"] == 0
    assert report["rewritten_qdrant_refs"] == 0

    stored = await get_memory_store().get(legacy_id)
    assert stored is None
    points = await get_qdrant()._client.retrieve(
        collection_name="test_memories",
        ids=[legacy_id],
        with_payload=True,
        with_vectors=False,
    )
    assert points
    assert points[0].payload["content"] == legacy_content


@pytest.mark.asyncio
async def test_project_task_service_uses_fresh_store_after_store_reset(client) -> None:
    from app.dependencies import get_qdrant
    import app.services.project_tasks_store as project_tasks_store_module

    old_store = project_tasks_store_module._STORE
    if old_store is not None:
        old_store.close()
    project_tasks_store_module._STORE = ProjectTasksStore(Path(":memory:"))

    items = await list_project_tasks(get_qdrant(), project="alpha", status="all", limit=5)
    assert items == []


@pytest.mark.asyncio
async def test_backfill_tasks_from_improvements_creates_missing_task_entities(client) -> None:
    improvement_id = str(await get_improvements_store().insert(
        title="Legacy autodocs migration",
        description="Historical improvement should appear as a canonical task after backfill.",
        project="alpha",
        agent_id="architect",
        importance_score=0.8,
        tags=["autodocs"],
    ))

    listed_before = await client.get("/api/v1/artifacts?project=alpha&type=task")
    assert listed_before.status_code == 200, listed_before.text
    assert all(item["task_id"] != improvement_id for item in listed_before.json()["items"])

    backfill = await client.post("/api/v1/project/tasks/backfill-from-improvements?project=alpha")
    assert backfill.status_code == 200, backfill.text
    report = backfill.json()
    assert report["project"] == "alpha"
    assert report["created"] >= 1
    assert report["failed"] == 0

    listed = await client.get("/api/v1/artifacts?project=alpha&type=task")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(item["task_id"] == improvement_id for item in items)

    second = await client.post("/api/v1/project/tasks/backfill-from-improvements?project=alpha")
    assert second.status_code == 200, second.text
    second_report = second.json()
    assert second_report["created"] == 0
    assert second_report["skipped_existing"] >= 1
    assert second_report["failed"] == 0


@pytest.mark.asyncio
async def test_backfill_normalizes_mojibake_titles_and_descriptions(client) -> None:
    original_title = (
        "\u0410\u0440\u0445\u0438\u0442\u0435\u043a\u0442\u0443\u0440\u043d\u044b\u0439 "
        "\u0440\u0435\u0444\u0430\u043a\u0442\u043e\u0440\u0438\u043d\u0433 "
        "\u0445\u0440\u0430\u043d\u0438\u043b\u0438\u0449"
    )
    original_description = (
        "\u0412\u043e\u0437\u043c\u043e\u0436\u043d\u044b\u0435 "
        "\u043f\u0440\u0438\u0447\u0438\u043d\u044b: "
        "\u0441\u0435\u0440\u0432\u0435\u0440 \u043d\u0435 "
        "\u0437\u0430\u043f\u0443\u0449\u0435\u043d"
    )
    mojibake_title = original_title.encode("utf-8").decode("cp1251")
    mojibake_description = original_description.encode("utf-8").decode("cp1251")
    improvement_id = str(await get_improvements_store().insert(
        title=mojibake_title,
        description=mojibake_description,
        project="beta",
        agent_id="architect",
        importance_score=0.8,
        tags=["autodocs"],
    ))

    backfill = await client.post("/api/v1/project/tasks/backfill-from-improvements?project=beta")
    assert backfill.status_code == 200, backfill.text
    assert backfill.json()["failed"] == 0

    task = await client.get(f"/api/v1/project/tasks/{improvement_id}?project=beta")
    assert task.status_code == 200, task.text
    body = task.json()
    assert body["title"] == original_title
    assert body["description"] == original_description


@pytest.mark.asyncio
async def test_backfill_clips_oversized_legacy_text_fields(client) -> None:
    long_title = "T" * 400
    long_description = "D" * 12050
    improvement_id = str(await get_improvements_store().insert(
        title=long_title,
        description=long_description,
        project="gamma",
        agent_id="architect",
        importance_score=0.7,
        tags=["legacy"],
    ))

    backfill = await client.post("/api/v1/project/tasks/backfill-from-improvements?project=gamma")
    assert backfill.status_code == 200, backfill.text
    report = backfill.json()
    assert report["failed"] == 0
    assert report["created"] >= 1

    task = await client.get(f"/api/v1/project/tasks/{improvement_id}?project=gamma")
    assert task.status_code == 200, task.text
    body = task.json()
    assert len(body["title"]) == 256
    assert body["title"] == long_title[:256]
    assert len(body["description"]) <= 10000
    assert body["description"] == long_description[:len(body["description"])]
    assert len(body["title"]) + 2 + len(body["description"]) <= 10000


@pytest.mark.asyncio
async def test_backfill_skips_malformed_legacy_rows_and_continues(client, monkeypatch) -> None:
    from app.dependencies import get_ollama, get_qdrant

    class FakeStore:
        async def list(self, project=None, status=None, limit=500):
            return [
                {
                    "id": "legacy-bad",
                    "title": "No project field here",
                    "description": "Malformed legacy row",
                    "status": "open",
                },
                {
                    "id": "legacy-good",
                    "project": "delta",
                    "title": "Valid legacy row",
                    "description": "Should still become task",
                    "status": "open",
                    "tags": ["legacy"],
                },
            ]

    monkeypatch.setattr("app.services.project_task_service.get_improvements_store", lambda: FakeStore())

    report = await backfill_tasks_from_improvements(
        get_qdrant(),
        get_ollama(),
        project=None,
        limit=20,
    )
    assert report.failed == 1
    assert "legacy-bad" in report.failed_task_ids
    assert report.created == 1

    task = await client.get("/api/v1/project/tasks/legacy-good?project=delta")
    assert task.status_code == 200, task.text
