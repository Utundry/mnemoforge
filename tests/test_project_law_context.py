import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from app.dependencies import get_ollama, get_qdrant
from app.config import settings
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.models.docs import DocsSection, DocsStatus
from app.services import docs_service
from app.services.learning_store import get_learning_store, make_context_signature
from app.services.project_context_service import gather_project_knowledge_snapshot
from app.services.project_knowledge import ProjectKnowledgeService

PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_enrich_task_includes_active_laws_before_components(client, monkeypatch):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Require law review",
        "statement": "Agents must review active project laws before risky changes.",
        "rationale": "Prevents drift from reviewed project rules.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created.status_code == 201

    async def fake_search(self, project_id, query, limit):
        return [{
            "component_id": "worker",
            "name": "Worker",
            "_score": 0.9,
            "purpose": "Executes background jobs.",
            "implementation": "Consumes queue tasks.",
            "status": "working",
            "endpoints": ["/tasks"],
            "key_files": ["app/services/job_queue.py"],
            "version_note": "",
        }]

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Add a risky background worker change",
        "max_components": 3,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["laws"]) == 1
    assert data["laws"][0]["title"] == "Require law review"
    assert "## Applicable Project Laws" in data["context"]
    assert "Require law review" in data["context"]
    assert "### Worker (worker)" in data["context"]
    assert data["context"].index("## Applicable Project Laws") < data["context"].index("### Worker (worker)")


@pytest.mark.asyncio
async def test_enrich_task_returns_laws_when_components_are_missing(client, monkeypatch):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Keep audit trail",
        "statement": "Agents must document rationale for impactful project changes.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created.status_code == 201

    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Plan a project change",
        "max_components": 3,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["components"] == []
    assert len(data["laws"]) == 1
    assert data["laws"][0]["title"] == "Keep audit trail"
    assert "Keep audit trail" in data["context"]
    assert data["message"] == "No relevant components found. Returned applicable project laws only."


@pytest.mark.asyncio
async def test_enrich_task_handoff_compact_uses_compact_law_projection(client, monkeypatch):
    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Keep audit trail",
        "statement": "Agents must document rationale for impactful project changes.",
        "rationale": "This prevents silent architecture drift and makes follow-up review faster by preserving decision intent in durable project memory.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created.status_code == 201

    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Plan a project change",
        "max_components": 3,
        "context_profile": "handoff_compact",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["detail"] == "compact"
    assert data["context_profile"] == "handoff_compact"
    assert "## Applicable Project Laws" in data["context"]
    assert "Keep audit trail" in data["context"]
    assert "Why:" in data["context"]
    assert "## Available Layers" in data["context"]
    assert data["available_layers"]["laws"]["count"] == 1
    assert data["token_budget"]["basis"] == "model_context_window_ratio"

    full_resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Plan a project change",
        "max_components": 3,
        "context_profile": "handoff_compact",
        "detail": "full",
    })
    assert full_resp.status_code == 200
    full_data = full_resp.json()
    assert full_data["detail"] == "full"
    assert "## Available Layers" not in full_data["context"]


@pytest.mark.asyncio
async def test_enrich_task_hot_path_defers_heavy_synthesis_sources(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("Heavy synthesis source should not be called in hot_path profile")

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)
    monkeypatch.setattr("app.services.project_context_service._fetch_open_improvements", fail_if_called)
    monkeypatch.setattr("app.services.project_context_service._fetch_recent_memoirs", fail_if_called)
    monkeypatch.setattr("app.services.project_context_service._fetch_effective_doc_sections", fail_if_called)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Need startup context with deferred synthesis",
        "max_components": 3,
        "context_profile": "hot_path",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["improvements"] == []
    assert data["memoirs"] == []
    assert data["docs_sections"] == []
    assert data["deferred_sources"] == ["improvements", "memoirs", "docs_sections"]
    assert "improvements" not in data["missing_sources"]
    assert "memoirs" not in data["missing_sources"]
    assert "docs_sections" not in data["missing_sources"]
    assert "## Background Synthesis Deferred" in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_includes_promoted_canonicals_when_local_knowledge_is_sparse(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    async def fake_promoted_canonicals(*args, **kwargs):
        return [
            {
                "id": "canonical-1",
                "scope": "domain",
                "topic_path": "knowledge/context",
                "content": "Prefer unified retrieval over scattered endpoint-specific lookup.",
                "confidence": 0.94,
                "canonical_status": "active",
                "project": "mnemoforge",
                "timestamp": "2026-04-16T00:00:00+00:00",
            }
        ]

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)
    monkeypatch.setattr("app.services.project_context_service._fetch_promoted_canonicals", fake_promoted_canonicals)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Plan a sparse-project change",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["promoted_canonicals"] == [
        {
            "id": "canonical-1",
            "scope": "domain",
            "topic_path": "knowledge/context",
            "content": "Prefer unified retrieval over scattered endpoint-specific lookup.",
            "confidence": 0.94,
            "canonical_status": "active",
            "project": "mnemoforge",
            "timestamp": "2026-04-16T00:00:00+00:00",
        }
    ]
    assert "## Promoted Canonical Knowledge" in data["context"]
    assert "Prefer unified retrieval over scattered endpoint-specific lookup." in data["context"]
    assert "Included promoted canonicals as fallback knowledge." in data["message"]


@pytest.mark.asyncio
async def test_enrich_task_includes_active_operational_instincts(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Investigate sparse project context before reading code",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert any(item["instinct_id"] == "ask_memory_before_code" for item in data["operational_instincts"])
    assert "## Active Operational Instincts" in data["context"]
    assert "ask_memory_before_code" in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_exposes_recommended_mcp_calls_for_sparse_context(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    async def fake_empty(*args, **kwargs):
        return []

    async def fake_empty_triage(*args, **kwargs):
        return {"project_id": "alpha", "found": 0, "recommended_task_id": "", "items": []}

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)
    monkeypatch.setattr("app.services.project_context_service._fetch_open_improvements", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_runtime_hints", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_recent_memoirs", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_recent_tasks", fake_empty)
    monkeypatch.setattr("app.services.project_context_service.build_task_triage", fake_empty_triage)
    monkeypatch.setattr("app.services.project_context_service._fetch_task_capture_candidates", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_effective_doc_sections", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_promoted_canonicals", fake_empty)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Find the next best MCP call",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recommended_mcp_calls"][0]["tool"] == "list_open_tasks"
    assert data["recommended_mcp_calls"][1]["tool"] == "get_project_readiness"
    assert "## Recommended MCP Calls" in data["context"]
    assert "list_open_tasks" in data["context"]
    assert "tool_feedback" not in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_prefers_tool_family_index_for_mcp_selection_tasks(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    async def fake_empty(*args, **kwargs):
        return []

    async def fake_empty_triage(*args, **kwargs):
        return {"project_id": "alpha", "found": 0, "recommended_task_id": "", "items": []}

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)
    monkeypatch.setattr("app.services.project_context_service._fetch_open_improvements", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_runtime_hints", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_recent_memoirs", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_recent_tasks", fake_empty)
    monkeypatch.setattr("app.services.project_context_service.build_task_triage", fake_empty_triage)
    monkeypatch.setattr("app.services.project_context_service._fetch_task_capture_candidates", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_effective_doc_sections", fake_empty)
    monkeypatch.setattr("app.services.project_context_service._fetch_promoted_canonicals", fake_empty)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Choose the right MCP tool family for the next step",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recommended_mcp_calls"][0]["tool"] == "list_tool_families"
    assert data["recommended_mcp_calls"][1]["tool"] == "list_open_tasks"
    assert data["recommended_mcp_calls"][0]["follow_up"] == "tool_feedback"
    assert "tool_feedback" in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_includes_improvements_runtime_hints_and_memoirs(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return [{
            "component_id": "context",
            "name": "Context Assembly",
            "_score": 0.91,
            "purpose": "Builds project-aware context for agents.",
            "implementation": "Combines retrieval surfaces into one prompt bundle.",
            "status": "working",
            "endpoints": ["/project/enrich-task"],
            "key_files": ["app/routers/project.py"],
            "version_note": "",
        }]

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Use knowledge-first retrieval",
        "statement": "Agents should retrieve project context before reading code.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created.status_code == 201

    improvement = await client.post(f"{PREFIX}/improvements", json={
        "title": "Unify project context",
        "description": "Make enrich-task include more than component summaries.",
        "project": "alpha",
        "agent_id": "codex",
        "importance_score": 0.8,
        "tags": ["autodocs"],
    })
    assert improvement.status_code == 201

    await get_learning_store().insert_artifact(
        agent_id="codex",
        artifact_type="workflow_guidance",
        scope="runtime_hint",
        status="active",
        content="Need reusable guidance for assembling project context from memory.",
        confidence=0.84,
        evidence_count=4,
        tags=["project:alpha", "autodocs"],
        action_type="suggest_create_improvement",
        context_signature=make_context_signature(
            project="alpha",
            task_type="architecture",
            phase="analysis",
            category="project_context",
            transport="api",
        ),
        observation="Repeated gap around context assembly.",
        why_it_matters="Agents should start from memory, not raw code search.",
    )

    qdrant = get_qdrant()
    ollama = get_ollama()
    memoir = MemoryCreate(
        content="# Memoir: Context assembly\n\nSettled on a unified retrieval bundle for agent startup.",
        agent_id="codex",
        memory_type=MemoryType.experience,
        category="task_memoir",
        source="memoir:test-task-1",
        tags=["task_id:test-task-1", "memoir", "project:alpha"],
        project="alpha",
        scope="project",
    )
    await qdrant.insert(memoir, await ollama.embed(memoir.content))

    task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-1",
        "project": "alpha",
        "title": "Implement unified project context",
        "description": "Make task enrichment consume more than component docs.",
        "agent_id": "codex",
        "status": "active",
    })
    assert task.status_code == 201, task.text
    task_change = await client.post(f"{PREFIX}/project/tasks/alpha-task-1/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Added improvements, runtime hints, memoirs, and tasks to enrich-task.",
        "why": "Unified retrieval should expose current project work and rationale.",
        "agent_id": "codex",
    })
    assert task_change.status_code == 201, task_change.text

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Redesign autodocumentation around unified retrieval",
        "max_components": 3,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["components"]) == 1
    assert len(data["laws"]) == 1
    assert len(data["improvements"]) == 1
    assert len(data["runtime_hints"]) == 1
    assert len(data["memoirs"]) == 1
    assert len(data["tasks"]) == 1
    assert data["coverage"]["components"] == 1
    assert data["coverage"]["docs_sections"] == 0
    assert "docs_sections" in data["missing_sources"]
    assert data["code_inspection_recommended"] is False
    assert "## Open Improvements" in data["context"]
    assert "## Active Runtime Hints" in data["context"]
    assert "## Recent Decision Memoirs" in data["context"]
    assert "## Recent Project Tasks" in data["context"]
    assert "Unify project context" in data["context"]
    assert "Context assembly" in data["context"]
    assert "Implement unified project context" in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_includes_task_capture_candidates(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-capture-enrich",
        "project": "alpha",
        "title": "Wire capture drafts into enrich-task",
        "description": "Keep task capture drafts visible before they become project truth.",
        "agent_id": "codex",
        "status": "active",
    })
    assert task.status_code == 201, task.text

    task_change = await client.post(f"{PREFIX}/project/tasks/alpha-task-capture-enrich/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Added task capture completion and persisted draft artifacts.",
        "why": "Enrich-task should surface cheap draft framing alongside canonical artifacts.",
        "agent_id": "codex",
    })
    assert task_change.status_code == 201, task_change.text

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["Task capture drafts stay reviewable until promotion."],
                "constraint": ["Cheap local capture should appear before cloud-heavy synthesis."],
                "definition_of_done": ["Enrich-task includes recent draft capture artifacts."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    capture = await client.post(
        "/api/v1/project/tasks/alpha-task-capture-enrich/capture-candidates?project=alpha"
    )
    assert capture.status_code == 200, capture.text
    capture_body = capture.json()
    assert capture_body["persisted_count"] >= 1

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Use draft task framing before promotion",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["task_capture_candidates"]) >= 1
    assert any(item["kind"] == "assumption" for item in data["task_capture_candidates"])
    assert "## Task Capture Drafts" in data["context"]
    assert "alpha-task-capture-enrich" in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_includes_specialized_task_capture_candidates(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-capture-specialized-enrich",
        "project": "alpha",
        "title": "Expose specialized capture drafts in enrich-task",
        "description": "Done task with richer capture artifacts.",
        "agent_id": "codex",
        "status": "done",
    })
    assert task.status_code == 201, task.text

    await client.post(f"{PREFIX}/project/tasks/alpha-task-capture-specialized-enrich/changes", json={
        "project": "alpha",
        "change_type": "decision",
        "content": "Use richer task capture artifacts before memoir generation.",
        "why": "Need decision traceability.",
        "agent_id": "codex",
    })
    await client.post(f"{PREFIX}/project/tasks/alpha-task-capture-specialized-enrich/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Touched app/services/task_capture_service.py:42 for specialized capture persistence.",
        "why": "Need code-link extraction.",
        "agent_id": "codex",
    })
    await client.post(f"{PREFIX}/project/deferred-findings", json={
        "project_id": "alpha",
        "task_id": "alpha-task-capture-specialized-enrich",
        "finding": "Need one more risk review pass.",
        "suggested_follow_up": "Inspect remaining-risk promotion.",
        "why_it_matters": "Done task still carries explicit remaining risk.",
        "severity": "medium",
        "agent_id": "codex",
    })

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps({"verification_result": "Validated with focused regression coverage."}, ensure_ascii=False)

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    capture = await client.post(
        "/api/v1/project/tasks/alpha-task-capture-specialized-enrich/capture-candidates?project=alpha"
    )
    assert capture.status_code == 200, capture.text

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Use richer task capture context before memoir generation",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert any(item["kind"] == "code_link" for item in data["task_capture_candidates"])
    assert any(item["kind"] == "remaining_risk" for item in data["task_capture_candidates"])


@pytest.mark.asyncio
async def test_enrich_task_prioritizes_incomplete_task_statements_in_recent_tasks(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    complete_task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-complete-framing",
        "project": "alpha",
        "title": "Complete framing task",
        "description": "\n".join(
            [
                "Ship the complete path.",
                "Assumption: canonical task state is already grounded.",
                "Constraint: keep retrieval deterministic.",
                "Definition of done: task statement remains stable.",
            ]
        ),
        "agent_id": "codex",
        "status": "active",
    })
    assert complete_task.status_code == 201, complete_task.text

    incomplete_task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-incomplete-framing",
        "project": "alpha",
        "title": "Incomplete framing task",
        "description": "This task still needs capture review help.",
        "agent_id": "codex",
        "status": "active",
    })
    assert incomplete_task.status_code == 201, incomplete_task.text

    await client.post(f"{PREFIX}/project/tasks/alpha-task-complete-framing/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Completed the stable framing path.",
        "why": "Keep a control task with grounded framing.",
        "agent_id": "codex",
    })
    await client.post(f"{PREFIX}/project/tasks/alpha-task-incomplete-framing/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Added a path that still needs cheap capture review.",
        "why": "The task is active but under-specified.",
        "agent_id": "codex",
    })

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["This task still has framing gaps that should be reviewed."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    capture = await client.post(
        "/api/v1/project/tasks/alpha-task-incomplete-framing/capture-candidates?project=alpha"
    )
    assert capture.status_code == 200, capture.text

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Choose the next project task to inspect",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["tasks"]) >= 2
    assert data["task_triage"]["recommended_task_id"] == "alpha-task-incomplete-framing"
    assert "recommended_action" in data["task_triage"]["items"][0]
    assert data["task_triage"]["items"][0]["recommended_action"]
    assert data["tasks"][0]["task_id"] == "alpha-task-incomplete-framing"
    assert data["tasks"][0]["task_statement_incomplete"] is True
    assert data["tasks"][0]["task_capture_pending_count"] >= 1
    assert any(item["source_kind"] == "capture_review" for item in data["tasks"][0]["next_actions"])
    assert data["task_triage"]["items"][0]["task_id"] == "alpha-task-incomplete-framing"
    assert "[incomplete-framing:" in data["context"]
    assert "## Task Triage" in data["context"]
    assert "Next action: Review" in data["context"]
    assert "Recommended action: Review" in data["context"]


@pytest.mark.asyncio
async def test_task_triage_prioritizes_incomplete_active_tasks(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    complete_task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-triage-complete",
        "project": "alpha",
        "title": "Complete triage task",
        "description": "\n".join(
            [
                "Keep the control task grounded.",
                "Assumption: triage signals are already complete here.",
                "Constraint: keep routing cheap.",
                "Definition of done: task is already fully framed.",
            ]
        ),
        "agent_id": "codex",
        "status": "active",
    })
    assert complete_task.status_code == 201, complete_task.text

    incomplete_task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-triage-incomplete",
        "project": "alpha",
        "title": "Incomplete triage task",
        "description": "This task should rise because framing is still incomplete.",
        "agent_id": "codex",
        "status": "active",
    })
    assert incomplete_task.status_code == 201, incomplete_task.text

    await client.post(f"{PREFIX}/project/tasks/alpha-task-triage-complete/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Completed the fully framed control path.",
        "why": "Keep a grounded task in the candidate set.",
        "agent_id": "codex",
    })
    await client.post(f"{PREFIX}/project/tasks/alpha-task-triage-incomplete/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Started the path that still needs capture review.",
        "why": "Triage should bring this forward.",
        "agent_id": "codex",
    })

    async def fake_generate(ollama, prompt: str) -> str:
        return json.dumps(
            {
                "assumption": ["This task still has unresolved framing gaps."],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.services.task_capture_service._generate_local_capture_fill", fake_generate)

    capture = await client.post(
        "/api/v1/project/tasks/alpha-task-triage-incomplete/capture-candidates?project=alpha"
    )
    assert capture.status_code == 200, capture.text

    triage = await client.post(f"{PREFIX}/project/task-triage", json={
        "project_id": "alpha",
        "limit": 5,
    })
    assert triage.status_code == 200, triage.text
    body = triage.json()
    assert body["recommended_task_id"] == "alpha-task-triage-incomplete"
    assert body["items"][0]["task_id"] == "alpha-task-triage-incomplete"
    assert body["items"][0]["task_statement_incomplete"] is True
    assert body["items"][0]["task_capture_pending_count"] >= 1
    assert "incomplete_framing:" in body["items"][0]["triage_reasons"][0]
    assert any(reason == "active_task" for reason in body["items"][0]["triage_reasons"])


@pytest.mark.asyncio
async def test_enrich_task_filters_demo_and_weak_legacy_runtime_hints(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    await get_learning_store().insert_artifact(
        agent_id="codex",
        artifact_type="workflow_guidance",
        scope="runtime_hint",
        status="active",
        workflow_action="context-assembly",
        content="Use project context snapshot before raw code inspection.",
        confidence=0.9,
        evidence_count=4,
        tags=["project:alpha", "autodocs"],
        context_signature=make_context_signature(project="alpha", task_type="architecture"),
        observation="Context-first flow reduced startup thrash.",
        why_it_matters="Agents stay aligned with current project state.",
    )
    await get_learning_store().insert_artifact(
        agent_id="codex",
        artifact_type="workflow_guidance",
        scope="runtime_hint",
        status="active",
        workflow_action="demo-hint",
        content="Use this only in mnemoforge-demo environment.",
        confidence=0.95,
        evidence_count=3,
        tags=["project:alpha", "project:mnemoforge-demo", "demo"],
        context_signature=make_context_signature(project="alpha", task_type="architecture"),
    )
    await get_learning_store().insert_artifact(
        agent_id="codex",
        artifact_type="workflow_guidance",
        scope="runtime_hint",
        status="active",
        workflow_action="legacy-hint",
        content="Legacy fallback.",
        confidence=0.2,
        evidence_count=1,
        tags=["project:alpha", "legacy"],
        context_signature=make_context_signature(project="alpha", task_type="architecture"),
    )

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Assemble project context safely",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["runtime_hints"]) == 1
    assert data["runtime_hints"][0]["content"].startswith("Use project context snapshot")
    assert "mnemoforge-demo" not in data["context"].lower()
    assert "Legacy fallback." not in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_returns_english_canonical_projection_with_originals(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    async def fake_canonicalize(fields, *, allow_cloud=True):
        translated = {}
        for key, value in fields.items():
            if not value:
                translated[key] = value
            else:
                translated[key] = f"EN::{value}"
        return translated

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)
    monkeypatch.setattr("app.services.project_context_service.canonicalize_agent_fields_to_english", fake_canonicalize)

    improvement = await client.post(f"{PREFIX}/improvements", json={
        "title": "Нужен единый контекст",
        "description": "Собирать проектное знание из памяти, а не из grep.",
        "project": "gamma",
        "agent_id": "codex",
        "importance_score": 0.8,
        "tags": ["autodocs"],
    })
    assert improvement.status_code == 201

    await get_learning_store().insert_artifact(
        agent_id="codex",
        artifact_type="workflow_guidance",
        scope="runtime_hint",
        status="active",
        content="Нужна подсказка по контексту проекта.",
        confidence=0.84,
        evidence_count=2,
        tags=["project:gamma"],
        action_type="suggest_create_improvement",
        context_signature=make_context_signature(project="gamma", task_type="architecture"),
        observation="Повторяется пробел в проектном контексте.",
        why_it_matters="Агент должен начинать с памяти.",
    )

    qdrant = get_qdrant()
    ollama = get_ollama()
    memoir = MemoryCreate(
        content="# Memoir: Контекст проекта\n\nРешили собирать контекст из unified knowledge.",
        agent_id="codex",
        memory_type=MemoryType.experience,
        category="task_memoir",
        source="memoir:gamma-task-1",
        tags=["task_id:gamma-task-1", "memoir", "project:gamma"],
        project="gamma",
        scope="project",
    )
    await qdrant.insert(memoir, await ollama.embed(memoir.content))

    task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "gamma-task-1",
        "project": "gamma",
        "title": "Собрать единый контекст проекта",
        "description": "Перевести retrieval на memory-first path.",
        "agent_id": "codex",
        "status": "active",
    })
    assert task.status_code == 201, task.text
    task_change = await client.post(f"{PREFIX}/project/tasks/gamma-task-1/changes", json={
        "project": "gamma",
        "change_type": "implementation",
        "content": "Добавили unified context assembly.",
        "why": "Агент должен меньше читать код вручную.",
        "agent_id": "codex",
    })
    assert task_change.status_code == 201, task_change.text

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "gamma",
        "task": "Собрать knowledge-first context",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["improvements"][0]["title"].startswith("EN::")
    assert data["improvements"][0]["original"]["title"] == "Нужен единый контекст"
    assert data["runtime_hints"][0]["content"].startswith("EN::")
    assert data["runtime_hints"][0]["original"]["content"] == "Нужна подсказка по контексту проекта."
    assert data["memoirs"][0]["title"].startswith("EN::")
    assert data["memoirs"][0]["original"]["title"] == "Контекст проекта"
    assert data["tasks"][0]["title"].startswith("EN::")
    assert data["tasks"][0]["original"]["title"] == "Собрать единый контекст проекта"


@pytest.mark.asyncio
async def test_project_snapshot_accepts_raw_qdrant_client_for_tasks(client, monkeypatch):
    async def fake_list_components(self, project_id):
        assert project_id == "alpha"
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "list_components", fake_list_components)

    task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-task-raw-client",
        "project": "alpha",
        "title": "Use raw client snapshot path",
        "description": "Exercise docs rebuild path with raw AsyncQdrantClient.",
        "agent_id": "codex",
        "status": "active",
    })
    assert task.status_code == 201, task.text

    task_change = await client.post(f"{PREFIX}/project/tasks/alpha-task-raw-client/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Verified raw-client snapshot path can still list project tasks.",
        "why": "Docs rebuild passes AsyncQdrantClient plus collection, not service wrapper.",
        "agent_id": "codex",
    })
    assert task_change.status_code == 201, task_change.text

    snapshot = await gather_project_knowledge_snapshot(
        project_id="alpha",
        qdrant=get_qdrant()._client,
        collection=settings.qdrant_collection_name,
        ollama=None,
        max_tasks=5,
    )
    assert any(item["task_id"] == "alpha-task-raw-client" for item in snapshot["tasks"])


@pytest.mark.asyncio
async def test_enrich_task_includes_effective_doc_sections(client, monkeypatch):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"proj-law-docs-{uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    try:
        async def fake_search(self, project_id, query, limit):
            return []

        monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)
        generated_at = datetime.now(timezone.utc)

        docs_service._save_docs_cache(
            "alpha",
            DocsStatus(
                project="alpha",
                generated_at=generated_at,
                sections={
                    "overview": DocsSection(name="Overview", content="Alpha uses memory-first retrieval."),
                    "architecture": DocsSection(name="Architecture", content="Context assembly reads the unified knowledge model."),
                    "decisions": DocsSection(name="Decision Log", content="Chose docs as projection, not source of truth."),
                },
                candidate_generated_at=generated_at + timedelta(seconds=1),
                candidate_sections={
                    "overview": DocsSection(name="Overview", content="Candidate overview."),
                },
            ),
        )

        resp = await client.post(f"{PREFIX}/project/enrich-task", json={
            "project_id": "alpha",
            "task": "Understand current project design",
            "max_components": 3,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["docs_sections"]) == 3
        assert data["docs_sections"][0]["section_key"] == "overview"
        assert data["docs_sections"][0]["candidate_available"] is True
        assert data["docs_sections"][0]["projection_state"] == "candidate"
        assert data["coverage"]["docs_sections"] == 3
        assert data["code_inspection_recommended"] is False
        assert "## Current Documentation Projection" in data["context"]
        assert "Candidate overview." in data["context"]
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_enrich_task_can_read_doc_sections_from_memory_layer(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    async def fake_list_doc_sections(qdrant_client, collection, project, limit=20):
        assert project == "alpha"
        return [
            {
                "content": "Alpha docs come from the memory layer.",
                "timestamp": "2026-03-22T09:00:00Z",
                "meta": {
                    "section_key": "overview",
                    "section_name": "Overview",
                    "generated_at": "2026-03-22T09:00:00Z",
                    "candidate_available": False,
                },
            }
        ]

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)
    monkeypatch.setattr("app.services.project_context_service.list_doc_sections", fake_list_doc_sections)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Use docs from the memory layer",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["docs_sections"]) == 1
    assert data["docs_sections"][0]["section_key"] == "overview"
    assert data["docs_sections"][0]["content"] == "Alpha docs come from the memory layer."
    assert data["docs_sections"][0]["projection_state"] == "effective"
    assert data["coverage"]["components"] == 0
    assert data["coverage"]["docs_sections"] == 1
    assert data["code_inspection_recommended"] is False
    assert "## Current Documentation Projection" in data["context"]


@pytest.mark.asyncio
async def test_enrich_task_filters_weak_memoirs(client, monkeypatch):
    async def fake_search(self, project_id, query, limit):
        return []

    qdrant = get_qdrant()
    ollama = get_ollama()

    weak_memoir = MemoryCreate(
        content="## Task\n\nUnknown task\n\n_No changes recorded._",
        agent_id="codex",
        memory_type=MemoryType.experience,
        category="task_memoir",
        source="memoir:weak",
        tags=["task_id:weak-task", "memoir", "project:alpha"],
        project="alpha",
        scope="project",
        meta={"quality_status": "weak"},
    )
    grounded_memoir = MemoryCreate(
        content="# Memoir: Grounded decision\n\nKept docs as projection over memory-first knowledge.",
        agent_id="codex",
        memory_type=MemoryType.experience,
        category="task_memoir",
        source="memoir:grounded",
        tags=["task_id:good-task", "memoir", "project:alpha"],
        project="alpha",
        scope="project",
        meta={"quality_status": "grounded", "change_count": 2},
    )
    await qdrant.insert(weak_memoir, await ollama.embed(weak_memoir.content))
    await qdrant.insert(grounded_memoir, await ollama.embed(grounded_memoir.content))

    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Read memoir context",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["memoirs"]) == 1
    assert data["memoirs"][0]["title"] == "Grounded decision"
    assert data["memoirs"][0]["quality_status"] == "grounded"


@pytest.mark.asyncio
async def test_enrich_task_reads_generated_memoir_content_from_sqlite_backed_store(client, monkeypatch):
    from app.services import memoir_service

    async def fake_search(self, project_id, query, limit):
        return []

    qdrant = get_qdrant()
    ollama = get_ollama()

    task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-memoir-store-task",
        "project": "alpha",
        "title": "Hydrate memoirs from SQLite",
        "description": "Ensure enrich-task can read memoir content after ref-only Qdrant writes.",
        "agent_id": "codex",
        "status": "done",
    })
    assert task.status_code == 201, task.text

    task_change = await client.post(f"{PREFIX}/project/tasks/alpha-memoir-store-task/changes", json={
        "project": "alpha",
        "change_type": "implementation",
        "content": "Switched task memoir storage to SQLite plus Qdrant reference payloads.",
        "why": "Keep memoirs recoverable after Qdrant failures.",
        "agent_id": "codex",
    })
    assert task_change.status_code == 201, task_change.text

    monkeypatch.setattr("app.services.cloud_llm.cloud_available", lambda: False)
    monkeypatch.setattr(ProjectKnowledgeService, "search", fake_search)

    memoir_id = await memoir_service.generate_and_store_memoir(
        "alpha-memoir-store-task",
        qdrant._client,
        qdrant._collection,
        ollama,
        agent_id="codex",
        project="alpha",
    )
    assert memoir_id is not None

    resp = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Use memoir retrieval after ref-only storage",
        "max_components": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["memoirs"]) == 1
    assert data["memoirs"][0]["quality_status"] == "grounded"
    assert "Hydrate memoirs from SQLite" in data["memoirs"][0]["content"]


@pytest.mark.asyncio
async def test_project_readiness_reports_bootstrap_needed_when_knowledge_is_sparse(client, monkeypatch):
    async def fake_list_components(self, project_id):
        return []

    async def fake_list_doc_sections(qdrant_client, collection, project, limit=20):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "list_components", fake_list_components)
    monkeypatch.setattr("app.services.project_context_service.list_doc_sections", fake_list_doc_sections)

    resp = await client.post(f"{PREFIX}/project/readiness", json={"project_id": "alpha"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == "alpha"
    assert data["readiness_level"] == "bootstrap_needed"
    assert data["external_pilot_ready"] is False
    assert data["coverage"]["components"] == 0
    assert data["coverage"]["docs_sections"] == 0
    assert any(item["instinct_id"] == "trust_first" for item in data["operational_instincts"])
    assert any(item["instinct_id"] == "raw_is_not_knowledge" for item in data["operational_instincts"])
    assert data["code_inspection_recommended"] is True
    assert any("No component knowledge or effective documentation" in item for item in data["blocking_gaps"])
    assert any("Ingest or refresh project components" in item for item in data["recommended_actions"])


@pytest.mark.asyncio
async def test_project_readiness_get_alias_matches_post(client, monkeypatch):
    async def fake_list_components(self, project_id):
        return []

    async def fake_list_doc_sections(qdrant_client, collection, project, limit=20):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "list_components", fake_list_components)
    monkeypatch.setattr("app.services.project_context_service.list_doc_sections", fake_list_doc_sections)

    post_resp = await client.post(f"{PREFIX}/project/readiness", json={"project_id": "alpha"})
    assert post_resp.status_code == 200, post_resp.text

    get_resp = await client.get(f"{PREFIX}/project/readiness", params={"project_id": "alpha"})
    assert get_resp.status_code == 200, get_resp.text

    assert get_resp.json()["readiness_level"] == post_resp.json()["readiness_level"]
    assert get_resp.json()["coverage"] == post_resp.json()["coverage"]


@pytest.mark.asyncio
async def test_project_readiness_reports_pilot_ready_when_core_layers_exist(client, monkeypatch):
    async def fake_list_components(self, project_id):
        return [
            {
                "component_id": "context",
                "name": "Context Assembly",
                "purpose": "Builds unified project context.",
                "implementation": "Combines project knowledge sources.",
                "status": "working",
                "endpoints": ["/project/enrich-task"],
                "key_files": ["app/services/project_context_service.py"],
                "version_note": "",
            }
        ]

    async def fake_list_doc_sections(qdrant_client, collection, project, limit=20):
        return [
            {
                "content": "Project documentation is available.",
                "timestamp": "2026-03-22T12:00:00Z",
                "meta": {
                    "section_key": "overview",
                    "section_name": "Overview",
                    "generated_at": "2026-03-22T12:00:00Z",
                    "candidate_available": False,
                },
            }
        ]

    monkeypatch.setattr(ProjectKnowledgeService, "list_components", fake_list_components)
    monkeypatch.setattr("app.services.project_context_service.list_doc_sections", fake_list_doc_sections)

    created = await client.post(f"{PREFIX}/laws", json={
        "project": "alpha",
        "title": "Use memory-first retrieval",
        "statement": "Agents should retrieve project context before reading code.",
        "agent_id": "codex",
        "status": "active",
        "confirmed_by": "user",
    })
    assert created.status_code == 201

    improvement = await client.post(f"{PREFIX}/improvements", json={
        "title": "Bootstrap external project workflow",
        "description": "Make readiness visible before external pilot work starts.",
        "project": "alpha",
        "agent_id": "codex",
        "importance_score": 0.8,
        "tags": ["bootstrap"],
    })
    assert improvement.status_code == 201

    task = await client.post(f"{PREFIX}/project/tasks", json={
        "task_id": "alpha-bootstrap",
        "project": "alpha",
        "title": "Bootstrap project knowledge",
        "description": "Prepare the project for memory-first retrieval.",
        "agent_id": "codex",
        "status": "active",
    })
    assert task.status_code == 201, task.text

    resp = await client.post(f"{PREFIX}/project/readiness", json={"project_id": "alpha"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["readiness_level"] == "pilot_ready"
    assert data["external_pilot_ready"] is True
    assert data["coverage"]["components"] == 1
    assert data["coverage"]["docs_sections"] == 1
    assert data["coverage"]["laws"] == 1
    assert data["coverage"]["improvements"] >= 1
    assert data["coverage"]["tasks"] >= 1
    assert data["blocking_gaps"] == []
    assert any(item == "Component knowledge is indexed." for item in data["strengths"])


@pytest.mark.asyncio
async def test_project_readiness_counts_bootstrap_task_entities_even_when_context_filters_them(client, monkeypatch):
    async def fake_list_components(self, project_id):
        return [
            {
                "component_id": "bootstrap",
                "name": "Bootstrap",
                "purpose": "Initial project knowledge entry.",
                "implementation": "Bootstraps project context.",
                "status": "working",
                "endpoints": [],
                "key_files": ["README.md"],
                "version_note": "",
            }
        ]

    async def fake_list_doc_sections(qdrant_client, collection, project, limit=20):
        return [
            {
                "content": "Project documentation is available.",
                "timestamp": "2026-03-22T12:00:00Z",
                "meta": {
                    "section_key": "overview",
                    "section_name": "Overview",
                    "generated_at": "2026-03-22T12:00:00Z",
                    "candidate_available": False,
                },
            }
        ]

    monkeypatch.setattr(ProjectKnowledgeService, "list_components", fake_list_components)
    monkeypatch.setattr("app.services.project_context_service.list_doc_sections", fake_list_doc_sections)

    improvement = await client.post(f"{PREFIX}/improvements", json={
        "title": "Bootstrap external project workflow",
        "description": "Make readiness visible before external pilot work starts.",
        "project": "alpha",
        "agent_id": "codex",
        "importance_score": 0.8,
        "tags": ["bootstrap"],
    })
    assert improvement.status_code == 201, improvement.text

    readiness = await client.post(f"{PREFIX}/project/readiness", json={"project_id": "alpha"})
    assert readiness.status_code == 200, readiness.text
    readiness_data = readiness.json()
    assert readiness_data["coverage"]["tasks"] >= 1

    enrich = await client.post(f"{PREFIX}/project/enrich-task", json={
        "project_id": "alpha",
        "task": "Inspect project state",
        "max_components": 3,
    })
    assert enrich.status_code == 200, enrich.text
    enrich_data = enrich.json()
    assert enrich_data["tasks"] == []


@pytest.mark.asyncio
async def test_project_bootstrap_checklist_orders_pending_steps(client, monkeypatch):
    async def fake_list_components(self, project_id):
        return []

    async def fake_list_doc_sections(qdrant_client, collection, project, limit=20):
        return []

    monkeypatch.setattr(ProjectKnowledgeService, "list_components", fake_list_components)
    monkeypatch.setattr("app.services.project_context_service.list_doc_sections", fake_list_doc_sections)

    resp = await client.post(f"{PREFIX}/project/bootstrap-checklist", json={"project_id": "alpha"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == "alpha"
    assert data["bootstrap_ready"] is False
    assert data["next_step"] == "components_indexed"
    assert data["steps"][0]["step_id"] == "snapshot_attached"
    assert data["steps"][0]["status"] == "recommended"
    assert data["steps"][1]["step_id"] == "components_indexed"
    assert data["steps"][1]["status"] == "pending"
    assert data["steps"][2]["step_id"] == "docs_projected"
    assert data["steps"][2]["status"] == "pending"
    assert any(item["instinct_id"] == "project_scope_first" for item in data["operational_instincts"])


@pytest.mark.asyncio
async def test_project_ingest_persists_snapshot_metadata_and_readiness_exposes_it(client, monkeypatch, tmp_path):
    source = tmp_path / "component.py"
    source.write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    async def fake_summary(ollama, name, files, root=""):
        return {
            "purpose": "Runs a component.",
            "implementation": "Simple implementation.",
            "status": "working",
            "version_note": "",
        }

    async def fake_list_doc_sections(qdrant_client, collection, project, limit=20):
        return []

    monkeypatch.setattr("app.routers.project._summarize_component", fake_summary)
    monkeypatch.setattr("app.services.project_context_service.list_doc_sections", fake_list_doc_sections)

    ingest = await client.post(f"{PREFIX}/project/ingest", json={
        "project_id": "alpha",
        "project_name": "Alpha",
        "components": [
            {
                "component_id": "runner",
                "name": "Runner",
                "files": [str(source)],
                "endpoints": [],
            }
        ],
        "snapshot": {
            "source_mode": "git_snapshot",
            "repo": "https://github.com/example/alpha",
            "branch": "main",
            "commit_sha": "abc123def456",
            "diff_summary": "Initial external-project import.",
            "pr_ref": "PR-17",
        },
    })
    assert ingest.status_code == 200, ingest.text
    ingest_data = ingest.json()
    assert ingest_data["snapshot"]["commit_sha"] == "abc123def456"

    components = await client.get(f"{PREFIX}/project/components", params={"project_id": "alpha"})
    assert components.status_code == 200, components.text
    comp_data = components.json()
    assert comp_data["count"] == 1
    assert comp_data["components"][0]["snapshot"]["repo"] == "https://github.com/example/alpha"
    assert comp_data["components"][0]["snapshot"]["commit_sha"] == "abc123def456"

    readiness = await client.post(f"{PREFIX}/project/readiness", json={"project_id": "alpha"})
    assert readiness.status_code == 200, readiness.text
    readiness_data = readiness.json()
    assert readiness_data["snapshot"]["repo"] == "https://github.com/example/alpha"
    assert readiness_data["snapshot"]["branch"] == "main"
    assert readiness_data["snapshot"]["commit_sha"] == "abc123def456"
    assert readiness_data["snapshot"]["source_mode"] == "git_snapshot"

    checklist = await client.post(f"{PREFIX}/project/bootstrap-checklist", json={"project_id": "alpha"})
    assert checklist.status_code == 200, checklist.text
    checklist_data = checklist.json()
    assert checklist_data["steps"][0]["step_id"] == "snapshot_attached"


async def test_remote_snapshot_plan_prefers_diff_skip_for_clean_git_snapshot(client):
    response = await client.post(
        "/api/v1/project/remote-snapshot/plan",
        json={
            "project_id": "alpha",
            "storage_mode": "knowledge_only",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "abc123def456",
                "base_commit_sha": "abc123def456",
                "dirty_workspace": False,
                "diff_summary": "",
            },
            "changed_files": ["app/main.py", "app/main.py"],
            "deleted_files": [],
            "renamed_files": [],
            "files": [],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["storage_mode"] == "knowledge_only"
    assert data["snapshot"]["source_mode"] == "git_snapshot"
    assert data["counts"]["changed_files"] == 1
    assert data["plan"]["rebuild_mode"] == "skip_if_unchanged"
    assert data["plan"]["projection_target_state"] == "effective"
    assert data["plan"]["requires_selective_source_payload"] is True
    assert data["contract"]["stores_full_repo_by_default"] is False
    assert data["contract"]["stores_selective_source_cache"] is False


async def test_remote_snapshot_plan_marks_dirty_workspace_as_candidate_overlay(client):
    response = await client.post(
        "/api/v1/project/remote-snapshot/plan",
        json={
            "project_id": "alpha",
            "storage_mode": "selective_source_cache",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "def789abc000",
                "base_commit_sha": "abc123def456",
                "dirty_workspace": True,
                "diff_summary": "Working tree contains local edits.",
            },
            "changed_files": ["app/main.py", "app/routes.py"],
            "deleted_files": ["app/legacy.py"],
            "renamed_files": [{"from_path": "app/old.py", "to_path": "app/new.py"}],
            "files": [
                {
                    "path": "app/main.py",
                    "status": "modified",
                    "content": "print('hello')",
                    "content_hash": "hash-main",
                    "language": "python",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["storage_mode"] == "selective_source_cache"
    assert data["counts"]["changed_files"] == 2
    assert data["counts"]["deleted_files"] == 1
    assert data["counts"]["renamed_files"] == 1
    assert data["counts"]["files_with_content"] == 1
    assert data["plan"]["rebuild_mode"] == "diff_only"
    assert data["plan"]["projection_target_state"] == "candidate"
    assert data["plan"]["requires_selective_source_payload"] is True
    assert "app/main.py" in data["normalized"]["file_payload_paths"]
    assert "app/routes.py" in data["plan"]["touched_paths"]
    assert "app/new.py" in data["plan"]["touched_paths"]
    assert data["contract"]["stores_selective_source_cache"] is True
    assert data["contract"]["full_mirror_enabled"] is False


async def test_project_refresh_remote_snapshot_skips_same_commit_without_root_dir(client, monkeypatch):
    class _FakeService:
        async def ensure_collection(self):
            return None

        async def list_components(self, project_id: str):
            assert project_id == "alpha"
            return [
                {
                    "component_id": "context",
                    "name": "Context",
                    "key_files": ["app/context.py"],
                    "file_hash": "hash-context",
                    "snapshot": {"commit_sha": "abc123def456"},
                }
            ]

    monkeypatch.setattr("app.routers.project.ProjectKnowledgeService", lambda *args, **kwargs: _FakeService())

    response = await client.post(
        "/api/v1/project/refresh",
        json={
            "project_id": "alpha",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "abc123def456",
                "dirty_workspace": False,
            },
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["updated"] == []
    assert data["up_to_date"] == ["context"]
    assert data["requires_source_payload"] == []


async def test_project_refresh_remote_snapshot_requests_source_payload_when_commit_changed(client, monkeypatch):
    class _FakeService:
        async def ensure_collection(self):
            return None

        async def list_components(self, project_id: str):
            assert project_id == "alpha"
            return [
                {
                    "component_id": "context",
                    "name": "Context",
                    "key_files": ["app/context.py"],
                    "file_hash": "hash-context",
                    "snapshot": {"commit_sha": "abc123def456"},
                }
            ]

    monkeypatch.setattr("app.routers.project.ProjectKnowledgeService", lambda *args, **kwargs: _FakeService())

    response = await client.post(
        "/api/v1/project/refresh",
        json={
            "project_id": "alpha",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "def789abc000",
                "dirty_workspace": False,
            },
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["updated"] == []
    assert data["up_to_date"] == []
    assert data["requires_source_payload"] == ["context"]


async def test_project_refresh_remote_snapshot_updates_component_from_selective_payload(client, monkeypatch):
    captured_upserts: list[dict] = []

    class _FakeService:
        async def ensure_collection(self):
            return None

        async def list_components(self, project_id: str):
            assert project_id == "alpha"
            return [
                {
                    "component_id": "context",
                    "name": "Context",
                    "key_files": ["app/context.py"],
                    "endpoints": [],
                    "file_hash": "hash-old",
                    "snapshot": {"commit_sha": "abc123def456"},
                }
            ]

        async def upsert_component(self, **kwargs):
            captured_upserts.append(kwargs)

        def compute_hash(self, file_contents: list[str]) -> str:
            assert any("print('remote change')" in item for item in file_contents)
            return "hash-new"

    class _FakeQueue:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def submit(self, job_type: str, payload: dict) -> str:
            self.calls.append((job_type, payload))
            return "job-docs-remote-1"

    fake_queue = _FakeQueue()

    async def fake_summarize_component_source(ollama, name: str, file_labels: list[str], source: str) -> dict:
        assert name == "Context"
        assert file_labels == ["app/context.py"]
        assert "print('remote change')" in source
        return {
            "purpose": "Remote context assembly",
            "implementation": "Updated from selective payload.",
            "status": "working",
            "version_note": "remote-refresh",
        }

    monkeypatch.setattr("app.routers.project.ProjectKnowledgeService", lambda *args, **kwargs: _FakeService())
    monkeypatch.setattr("app.routers.project._summarize_component_source", fake_summarize_component_source)
    monkeypatch.setattr("app.services.job_queue.get_job_queue", lambda: fake_queue)

    response = await client.post(
        "/api/v1/project/refresh",
        json={
            "project_id": "alpha",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "def789abc000",
                "dirty_workspace": False,
            },
            "changed_files": ["app/context.py"],
            "files": [
                {
                    "path": "app/context.py",
                    "status": "modified",
                    "content": "print('remote change')",
                    "content_hash": "hash-new",
                    "language": "python",
                    "component_hint": "context",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["updated"] == ["context"]
    assert data["up_to_date"] == []
    assert data["requires_source_payload"] == []
    assert data["used_remote_file_payload"] is True
    assert captured_upserts[0]["purpose"] == "Remote context assembly"
    assert captured_upserts[0]["file_hash"] == "hash-new"
    assert fake_queue.calls == [
        (
            "docs_rebuild",
            {
                "project": "alpha",
                "changed_component_ids": ["context"],
                "changed_files": ["app/context.py"],
            },
        )
    ]


async def test_remote_snapshot_sync_returns_skipped_for_same_commit(client, monkeypatch):
    class _FakeService:
        async def ensure_collection(self):
            return None

        async def list_components(self, project_id: str):
            assert project_id == "alpha"
            return [
                {
                    "component_id": "context",
                    "name": "Context",
                    "key_files": ["app/context.py"],
                    "file_hash": "hash-context",
                    "snapshot": {"commit_sha": "abc123def456"},
                }
            ]

    monkeypatch.setattr("app.routers.project.ProjectKnowledgeService", lambda *args, **kwargs: _FakeService())

    response = await client.post(
        "/api/v1/project/remote-snapshot/sync",
        json={
            "project_id": "alpha",
            "storage_mode": "knowledge_only",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "abc123def456",
                "base_commit_sha": "abc123def456",
                "dirty_workspace": False,
            },
            "changed_files": ["app/context.py"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["action"] == "skipped"
    assert data["plan"]["plan"]["rebuild_mode"] == "skip_if_unchanged"
    assert data["refresh"]["up_to_date"] == ["context"]


async def test_remote_snapshot_sync_returns_needs_source_payload_when_diff_has_no_files(client, monkeypatch):
    class _FakeService:
        async def ensure_collection(self):
            return None

        async def list_components(self, project_id: str):
            assert project_id == "alpha"
            return [
                {
                    "component_id": "context",
                    "name": "Context",
                    "key_files": ["app/context.py"],
                    "file_hash": "hash-context",
                    "snapshot": {"commit_sha": "abc123def456"},
                }
            ]

    monkeypatch.setattr("app.routers.project.ProjectKnowledgeService", lambda *args, **kwargs: _FakeService())

    response = await client.post(
        "/api/v1/project/remote-snapshot/sync",
        json={
            "project_id": "alpha",
            "storage_mode": "knowledge_only",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "def789abc000",
                "base_commit_sha": "abc123def456",
                "dirty_workspace": False,
            },
            "changed_files": ["app/context.py"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["action"] == "needs_source_payload"
    assert data["plan"]["plan"]["rebuild_mode"] == "diff_only"
    assert data["refresh"]["requires_source_payload"] == ["context"]


async def test_remote_snapshot_sync_returns_refreshed_when_selective_payload_is_present(client, monkeypatch):
    captured_upserts: list[dict] = []

    class _FakeService:
        async def ensure_collection(self):
            return None

        async def list_components(self, project_id: str):
            assert project_id == "alpha"
            return [
                {
                    "component_id": "context",
                    "name": "Context",
                    "key_files": ["app/context.py"],
                    "endpoints": [],
                    "file_hash": "hash-old",
                    "snapshot": {"commit_sha": "abc123def456"},
                }
            ]

        async def upsert_component(self, **kwargs):
            captured_upserts.append(kwargs)

        def compute_hash(self, file_contents: list[str]) -> str:
            return "hash-new"

    class _FakeQueue:
        async def submit(self, job_type: str, payload: dict) -> str:
            return "job-docs-remote-1"

    async def fake_summarize_component_source(ollama, name: str, file_labels: list[str], source: str) -> dict:
        return {
            "purpose": "Remote context assembly",
            "implementation": "Updated from selective payload.",
            "status": "working",
            "version_note": "remote-refresh",
        }

    monkeypatch.setattr("app.routers.project.ProjectKnowledgeService", lambda *args, **kwargs: _FakeService())
    monkeypatch.setattr("app.routers.project._summarize_component_source", fake_summarize_component_source)
    monkeypatch.setattr("app.services.job_queue.get_job_queue", lambda: _FakeQueue())

    response = await client.post(
        "/api/v1/project/remote-snapshot/sync",
        json={
            "project_id": "alpha",
            "storage_mode": "selective_source_cache",
            "snapshot": {
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/alpha",
                "branch": "main",
                "commit_sha": "def789abc000",
                "base_commit_sha": "abc123def456",
                "dirty_workspace": False,
            },
            "changed_files": ["app/context.py"],
            "files": [
                {
                    "path": "app/context.py",
                    "status": "modified",
                    "content": "print('remote change')",
                    "content_hash": "hash-new",
                    "language": "python",
                    "component_hint": "context",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["action"] == "refreshed"
    assert data["refresh"]["updated"] == ["context"]
    assert captured_upserts[0]["purpose"] == "Remote context assembly"


@pytest.mark.asyncio
async def test_bootstrap_from_project_memories_creates_initial_components(client):
    qdrant = get_qdrant()
    ollama = get_ollama()
    svc = ProjectKnowledgeService(qdrant._client, ollama)
    await svc.ensure_collection()

    await svc.upsert_component(
        project_id="alpha",
        component_id="1",
        name="1",
        purpose="Stale bootstrap artifact.",
        implementation="Old grouping from a bootstrap helper path.",
        key_files=["/proj/1/client_scan.py"],
        endpoints=[],
        status="wip",
        file_hash="stale-bootstrap",
        version_note="Bootstrapped from project-scoped remote client-scan memories.",
    )

    seed_items = [
        MemoryCreate(
            content="HAL core wiring for motion and spindle setup.",
            agent_id="remote",
            memory_type=MemoryType.context,
            category="config",
            source="client-scan:/proj/hal/core_motion.hal",
            tags=["project:alpha", "hal"],
            project="alpha",
            meta={"source_path": "/proj/hal/core_motion.hal"},
        ),
        MemoryCreate(
            content="Modbus relay board and spindle device configuration.",
            agent_id="remote",
            memory_type=MemoryType.context,
            category="setting",
            source="client-scan:/proj/modbus/modbus_config.json",
            tags=["project:alpha", "modbus"],
            project="alpha",
            meta={"source_path": "/proj/modbus/modbus_config.json"},
        ),
        MemoryCreate(
            content="[EMC]\nMACHINE = alpha\n",
            agent_id="remote",
            memory_type=MemoryType.context,
            category="config",
            source="client-scan:/proj/config.ini",
            tags=["project:alpha", "config"],
            project="alpha",
            meta={"source_path": "/proj/config.ini"},
        ),
        MemoryCreate(
            content="# bootstrap helper should not become a component",
            agent_id="remote",
            memory_type=MemoryType.context,
            category="context",
            source="client-scan:/proj/1/client_scan.py",
            tags=["project:alpha", "helper"],
            project="alpha",
            meta={"source_path": "/proj/1/client_scan.py"},
        ),
    ]
    for item in seed_items:
        await qdrant.insert(item, await ollama.embed(item.content))

    bootstrap = await client.post(f"{PREFIX}/project/bootstrap-from-memories", json={
        "project_id": "alpha",
        "root_hint": "/proj",
    })
    assert bootstrap.status_code == 200, bootstrap.text
    data = bootstrap.json()
    assert data["project_id"] == "alpha"
    assert data["created_components"] >= 2
    assert "project_runtime" in data["components"]
    assert "hal" in data["components"]
    assert "modbus" in data["components"]
    assert "1" not in data["components"]
    assert data["removed_components"] == ["1"]

    components = await client.get(f"{PREFIX}/project/components", params={"project_id": "alpha"})
    assert components.status_code == 200, components.text
    comp_data = components.json()
    assert comp_data["count"] >= 2
    component_ids = {item["component_id"] for item in comp_data["components"]}
    assert "project_runtime" in component_ids
    assert "hal" in component_ids
    assert "modbus" in component_ids
    assert "1" not in component_ids

    readiness = await client.post(f"{PREFIX}/project/readiness", json={"project_id": "alpha"})
    assert readiness.status_code == 200, readiness.text
    readiness_data = readiness.json()
    assert readiness_data["coverage"]["components"] >= 2


@pytest.mark.asyncio
async def test_project_readiness_auto_bootstraps_components_from_client_scan_memories(client):
    qdrant = get_qdrant()
    ollama = get_ollama()
    project_id = f"alpha-auto-bootstrap-{uuid4().hex[:8]}"

    seed_items = [
        MemoryCreate(
            content="HAL startup wiring for machine alpha.",
            agent_id="remote",
            memory_type=MemoryType.context,
            category="config",
            source="client-scan:/pilot/alpha/hal/core.hal",
            tags=[f"project:{project_id}", "hal"],
            project=project_id,
            meta={"source_path": "/pilot/alpha/hal/core.hal"},
        ),
        MemoryCreate(
            content="Spindle relay mapping and modbus transport settings.",
            agent_id="remote",
            memory_type=MemoryType.context,
            category="setting",
            source="client-scan:/pilot/alpha/modbus/modbus.json",
            tags=[f"project:{project_id}", "modbus"],
            project=project_id,
            meta={"source_path": "/pilot/alpha/modbus/modbus.json"},
        ),
    ]
    for item in seed_items:
        await qdrant.insert(item, await ollama.embed(item.content))

    readiness = await client.post(f"{PREFIX}/project/readiness", json={"project_id": project_id})
    assert readiness.status_code == 200, readiness.text
    body = readiness.json()
    assert body["coverage"]["components"] >= 2
    assert body["auto_bootstrap_from_memories"] is not None
    assert body["auto_bootstrap_from_memories"]["created_components"] >= 2
