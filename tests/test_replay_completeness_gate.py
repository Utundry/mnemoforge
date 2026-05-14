from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.routers import mcp_sse
from app.services.replay_completeness_service import (
    EXECUTION_READINESS_REQUIRED_EVIDENCE,
    REPLAY_COMPLETENESS_RELEASE_GATE,
    REPLAY_REQUIRED_FIELDS,
    build_replay_drill_decision,
    build_token_budget,
    evaluate_execution_readiness,
    evaluate_replay_completeness,
)


class _FakeRegistry:
    def log_handoff(self, **kwargs):
        pass

    def rank_for_task(self, task_type: str):
        return []


async def _wire_mcp_to_test_client(client, monkeypatch) -> None:
    async def local_get(api_base: str, path: str):
        response = await client.get(f"/api/v1{path}")
        assert response.status_code < 400, response.text
        return response.json()

    async def local_post(api_base: str, path: str, payload: dict):
        response = await client.post(f"/api/v1{path}", json=payload)
        assert response.status_code < 400, response.text
        return response.json()

    monkeypatch.setattr(mcp_sse, "_get", local_get)
    monkeypatch.setattr(mcp_sse, "_post", local_post)
    monkeypatch.setattr(mcp_sse, "_session_observe", AsyncMock())


@pytest.mark.asyncio
async def test_replay_completeness_v1_release_gate_reconstructs_fixture_project_from_mcp_state(client, monkeypatch):
    from app.routers import models as models_router
    from pathlib import Path
    from app.services import task_lease_service as lease_mod

    await _wire_mcp_to_test_client(client, monkeypatch)
    monkeypatch.setattr(models_router, "get_model_registry", lambda: _FakeRegistry())
    lease_store = lease_mod.TaskLeaseStore(Path(":memory:"))
    monkeypatch.setattr(lease_mod, "_STORE", lease_store)

    improvement = await client.post(
        "/api/v1/improvements",
        json={
            "title": "Release gate fixture project replay",
            "description": "\n".join(
                [
                    "Build a named release gate for MnemoForge replay completeness.",
                    "Assumption: durable MCP state is enough to resume the task.",
                    "Constraint: the replay path must not depend on old chat access.",
                    "Definition of done: replay_completeness_v1 is complete.",
                ]
            ),
            "project": "gate-project",
            "agent_id": "codex",
            "importance_score": 0.9,
            "tags": ["replay", "release-gate"],
        },
    )
    assert improvement.status_code == 201, improvement.text
    task_id = improvement.json()["id"]

    decision = await client.post(
        f"/api/v1/project/tasks/{task_id}/changes",
        json={
            "project": "gate-project",
            "change_type": "decision",
            "content": "Decision: replay gate must reconstruct linked improvement, task history, handoff, and context refs.",
            "why": "A project replay needs more than the last checkpoint.",
            "agent_id": "codex",
            "source": "release_gate_fixture",
            "tags": ["release_gate", "decision"],
        },
    )
    assert decision.status_code == 201, decision.text

    implementation = await client.post(
        f"/api/v1/project/tasks/{task_id}/changes",
        json={
            "project": "gate-project",
            "change_type": "implementation",
            "content": "Implemented replay_bundle assembly for pull_task_context.",
            "why": "Agents need compact durable state for mini-project continuation.",
            "agent_id": "codex",
            "source": "release_gate_fixture",
            "tags": ["release_gate", "implementation"],
        },
    )
    assert implementation.status_code == 201, implementation.text

    claimed = await mcp_sse._execute_tool(
        "claim_task",
        {
            "project": "gate-project",
            "task_id": task_id,
            "owner_agent": "codex",
            "session_id": "sess-gate",
        },
        "http://test",
    )
    assert json.loads(claimed)["status"] in {"claimed", "renewed"}

    checkpoint = await mcp_sse._execute_tool(
        "record_task_checkpoint",
        {
            "project": "gate-project",
            "task_id": task_id,
            "session_id": "sess-gate",
            "stage": "handoff",
            "status": "active",
            "summary": "Fixture project reached replay gate checkpoint.",
            "decisions": ["Run replay completeness as a named release gate."],
            "changed_files": ["app/services/replay_completeness_service.py", "tests/test_replay_completeness_gate.py"],
            "verification": ["Release gate fixture reconstructs required fields through MCP."],
            "remaining_risk": ["Broader multi-task project reproduction can extend this gate later."],
            "next_step": "Continue with the next implementation slice from replay_completeness_v1 output.",
            "acted_by": "codex",
            "to_agent": "codex",
        },
        "http://test",
    )
    assert "handoff_packet_created=True" in checkpoint
    released = await mcp_sse._execute_tool(
        "release_task_claim",
        {
            "lease_id": json.loads(claimed)["lease"]["lease_id"],
            "owner_agent": "codex",
            "session_id": "sess-gate",
            "reason": "checkpoint_recorded",
        },
        "http://test",
    )
    assert json.loads(released)["status"] == "released"

    replay = await mcp_sse._execute_tool(
        "pull_task_context",
        {
            "project": "gate-project",
            "task_id": task_id,
            "agent_id": "codex",
            "session_id": "sess-gate",
            "include_handoffs": True,
            "detail": "full",
        },
        "http://test",
    )
    data = json.loads(replay)

    assert data["replay_completeness"]["release_gate"] == REPLAY_COMPLETENESS_RELEASE_GATE
    assert data["replay_completeness"]["required_fields"] == REPLAY_REQUIRED_FIELDS
    assert data["replay_completeness"]["status"] == "complete"
    assert data["replay_completeness"]["missing_fields"] == []
    assert data["replay_completeness"]["can_continue_without_user"] is True
    assert data["task"]["title"] == "Release gate fixture project replay"
    assert data["task"]["linked_improvement_id"] == task_id
    assert data["latest_checkpoint"]["summary"] == "Fixture project reached replay gate checkpoint."
    assert data["latest_checkpoint"]["changed_files"] == [
        "app/services/replay_completeness_service.py",
        "tests/test_replay_completeness_gate.py",
    ]
    assert data["next_safe_action"] == "Continue with the next implementation slice from replay_completeness_v1 output."
    assert data["resume_handoffs"][0]["task_id"] == task_id

    bundle = data["replay_bundle"]
    assert bundle["linked_improvement"]["id"] == task_id
    assert bundle["linked_improvement"]["artifact_key"] == f"improvement:gate-project:{task_id}"
    assert bundle["linked_improvement"]["available"] is True
    assert bundle["linked_improvement"]["title"] == "Release gate fixture project replay"
    assert bundle["project_context_refs"]["project_id"] == "gate-project"
    assert bundle["project_context_refs"]["task_id"] == task_id
    assert bundle["project_context_refs"]["readiness_tool"] == "get_project_readiness"
    assert bundle["project_context_refs"]["enrichment_tool"] == "enrich_task_with_context"
    assert {"linked_improvement", "task_changes"} <= set(bundle["project_context_refs"]["grounded_by"])
    assert bundle["handoff_refs"][0]["task_id"] == task_id
    history_types = [item["change_type"] for item in bundle["task_history"]]
    assert "task_created" in history_types
    assert "decision" in history_types
    assert "implementation" in history_types
    assert "note" in history_types
    assert any("replay_bundle assembly" in item["content"] for item in bundle["task_history"])
    assert any("[task_checkpoint]" in item["content"] for item in bundle["task_history"])

    assert data["execution_readiness"]["status"] == "ready"
    assert data["execution_readiness"]["required_evidence"] == EXECUTION_READINESS_REQUIRED_EVIDENCE
    assert data["execution_readiness"]["missing_evidence"] == []
    assert data["execution_readiness"]["can_choose_next_action_without_user"] is True
    assert data["execution_readiness"]["recommended_next_tool"] == "pull_task_context"
    assert data["execution_readiness"]["recommended_next_action"] == data["next_safe_action"]
    assert data["replay_drill"]["status"] == "ready"
    assert data["replay_drill"]["first_tool"] == "enrich_task_with_context"
    assert data["replay_drill"]["first_action"] == data["next_safe_action"]
    assert data["replay_drill"]["tool_arguments"] == {
        "project_id": "gate-project",
        "task": data["next_safe_action"],
        "context_profile": "handoff_compact",
    }
    assert data["replay_drill"]["blocking_missing"] == []

    drill_context = await mcp_sse._execute_tool(
        data["replay_drill"]["first_tool"],
        data["replay_drill"]["tool_arguments"],
        "http://test",
    )
    assert drill_context.strip()
    assert "Recommended MCP calls:" in drill_context
    assert "get_project_readiness" in drill_context or "pull_task_context" in drill_context

    compact_replay = await mcp_sse._execute_tool(
        "pull_task_context",
        {
            "project": "gate-project",
            "task_id": task_id,
            "agent_id": "codex",
            "session_id": "sess-gate",
            "include_handoffs": True,
        },
        "http://test",
    )
    compact = json.loads(compact_replay)
    assert compact["detail"] == "compact"
    assert "replay_bundle" not in compact
    assert compact["replay_completeness"]["status"] == "complete"
    assert compact["execution_readiness"]["status"] == "ready"
    assert compact["replay_drill"]["first_tool"] == "enrich_task_with_context"
    assert compact["available_layers"]["task_history"]["count"] >= 4
    assert compact["available_layers"]["linked_improvement"]["available"] is True
    assert compact["available_layers"]["task_history"]["request"] == {"detail": "full"}
    assert compact["token_budget"]["basis"] == "model_context_window_ratio"
    assert compact["token_budget"]["profile"] == "normal"
    assert compact["token_budget"]["context_window"] == 32000
    assert compact["token_budget"]["estimated_tokens"] <= compact["token_budget"]["soft_limit_tokens"]
    lease_store.close()


def test_replay_completeness_v1_release_gate_fails_loudly_when_required_fields_are_missing():
    result = evaluate_replay_completeness(
        {
            "task": {"title": "Incomplete replay", "status": "active"},
            "latest_checkpoint": {"stage": "handoff", "summary": "Paused before verification."},
            "next_safe_action": "Record verification before continuing.",
        }
    )

    assert result["release_gate"] == REPLAY_COMPLETENESS_RELEASE_GATE
    assert result["status"] == "incomplete"
    assert result["can_continue_without_user"] is False
    assert result["missing_fields"] == [
        "latest_checkpoint.changed_files",
        "latest_checkpoint.verification",
    ]


def test_execution_readiness_fails_loudly_when_bundle_lacks_action_evidence():
    result = evaluate_execution_readiness(
        {
            "next_safe_action": "Continue with implementation.",
            "latest_checkpoint": {
                "stage": "handoff",
                "summary": "Paused after planning.",
                "verification": [],
                "remaining_risk": [],
            },
            "replay_bundle": {
                "linked_improvement": {"available": True},
                "task_history": [{"change_type": "decision", "content": "Use durable replay state."}],
                "handoff_refs": [],
                "project_context_refs": {"readiness_tool": "get_project_readiness"},
            },
        }
    )

    assert result["status"] == "incomplete"
    assert result["can_choose_next_action_without_user"] is False
    assert result["recommended_next_tool"] == "record_task_checkpoint"
    assert result["missing_evidence"] == [
        "implementation_history",
        "verification_evidence",
        "risk_evidence",
        "handoff_refs",
        "project_context_refs",
    ]


def test_execution_readiness_counts_checkpoint_stage_as_implementation_history():
    result = evaluate_execution_readiness(
        {
            "next_safe_action": "Use handoff checkpoint for operator review.",
            "latest_checkpoint": {
                "stage": "handoff",
                "summary": "Ready for handoff.",
                "verification": ["unit test passed"],
                "remaining_risk": ["feedback pending"],
            },
            "replay_bundle": {
                "linked_improvement": {"available": True},
                "task_history": [
                    {"change_type": "decision", "content": "Use durable replay state."},
                    {
                        "change_type": "note",
                        "content": "[task_checkpoint]\nCheckpoint stage: in_progress\nSummary: Implemented the facade.",
                        "tags": ["task_checkpoint", "task_stage:in_progress"],
                    },
                ],
                "handoff_refs": [{"memory_id": "handoff-1"}],
                "project_context_refs": {
                    "readiness_tool": "get_project_readiness",
                    "enrichment_tool": "enrich_task_with_context",
                },
            },
        }
    )

    assert result["status"] == "ready"
    assert result["evidence"]["implementation_history"] is True
    assert result["missing_evidence"] == []


def test_replay_drill_selects_checkpoint_when_replay_is_incomplete():
    decision = build_replay_drill_decision(
        {
            "project": "alpha",
            "task_id": "task-weak",
            "task": {"title": "Weak replay", "status": "active"},
            "latest_checkpoint": None,
            "next_safe_action": "Continue implementation.",
        }
    )

    assert decision["status"] == "blocked"
    assert decision["first_tool"] == "record_task_checkpoint"
    assert decision["tool_arguments"]["project"] == "alpha"
    assert decision["tool_arguments"]["task_id"] == "task-weak"
    assert "latest_checkpoint.stage" in decision["blocking_missing"]


def test_replay_drill_selects_enrichment_tool_from_ready_pull_task_context_output():
    decision = build_replay_drill_decision(
        {
            "project": "alpha",
            "task_id": "task-ready",
            "task": {"title": "Ready replay", "status": "active"},
            "next_safe_action": "Implement the next deterministic slice.",
            "latest_checkpoint": {
                "stage": "handoff",
                "summary": "Ready",
                "changed_files": ["app/services/replay_completeness_service.py"],
                "verification": ["unit test passed"],
                "remaining_risk": ["broader scenario pending"],
            },
            "replay_bundle": {
                "linked_improvement": {"available": True},
                "task_history": [
                    {"change_type": "decision", "content": "Use deterministic selector."},
                    {"change_type": "implementation", "content": "Added selector."},
                ],
                "handoff_refs": [{"memory_id": "handoff-1"}],
                "project_context_refs": {
                    "project_id": "alpha",
                    "task_id": "task-ready",
                    "readiness_tool": "get_project_readiness",
                    "enrichment_tool": "enrich_task_with_context",
                },
            },
        }
    )

    assert decision["status"] == "ready"
    assert decision["first_tool"] == "enrich_task_with_context"
    assert decision["tool_arguments"] == {
        "project_id": "alpha",
        "task": "Implement the next deterministic slice.",
        "context_profile": "handoff_compact",
    }


def test_token_budget_uses_context_window_ratio_with_soft_overflow():
    budget = build_token_budget(
        response_chars=4_600,
        model_context_window=20_000,
        resume_budget_profile="normal",
        min_floor=800,
        hard_cap=6_000,
        soft_overflow_ratio=0.20,
    )

    assert budget["basis"] == "model_context_window_ratio"
    assert budget["budget_tokens"] == 1000
    assert budget["soft_limit_tokens"] == 1200
    assert budget["estimated_tokens"] == 1150
    assert budget["within_budget"] is False
    assert budget["within_soft_limit"] is True
    assert budget["overflow_tokens"] == 150
    assert budget["overflow_reason"]


def test_token_budget_clamps_to_floor_and_hard_cap():
    floor_budget = build_token_budget(response_chars=100, model_context_window=4_000, resume_budget_ratio=0.01)
    cap_budget = build_token_budget(response_chars=100, model_context_window=200_000, resume_budget_ratio=0.10)

    assert floor_budget["budget_tokens"] == 800
    assert cap_budget["budget_tokens"] == 6000
