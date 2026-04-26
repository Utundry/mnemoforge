"""
MCP SSE Transport for FastAPI (spec 2024-11-05).

Allows zero-config client connection — no Python needed on the client:

    claude mcp add --transport sse -s user super-memory http://<SERVER_IP>:8000/mcp/sse

Protocol:
  GET  /mcp/sse                      — open SSE stream, receive endpoint URL
  POST /mcp/messages?sessionId=<id>  — send JSON-RPC requests
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from copy import deepcopy
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from app.services.operational_instincts_service import build_operational_instinct_playbook
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.services.mcp_tool_contracts import (
    build_approve_learning_candidate_payload,
    build_coordination_status_payload,
    build_defer_learning_candidate_payload,
    build_enrich_task_payload,
    build_list_artifacts_query,
    build_list_learning_candidates_query,
    build_list_open_tasks_query,
    build_list_coordination_query,
    build_normalize_mcp_intent_payload,
    build_pickup_coordination_payload,
    build_project_workflow_payload,
    build_project_workflow_submit_payload,
    build_project_workflow_submit_plan,
    build_project_bootstrap_payload,
    build_project_readiness_payload,
    build_remote_snapshot_payload,
    build_merge_canonicals_payload,
    build_reject_learning_candidate_payload,
    build_reopen_task_payload,
    build_send_coordination_message_payload,
    build_supermemory_initialize_hint,
    build_supermemory_onboarding_basics,
    build_report_task_checkpoint_payload,
    format_coordination_list,
    format_coordination_message,
    build_set_canonical_status_payload,
    format_learning_candidate_transition,
    format_list_learning_candidates_response,
    format_list_open_tasks_response,
    format_list_tool_families_response,
    format_tool_family_tools_response,
    format_tool_explain_response,
    format_tool_recommend_response,
    format_tool_feedback_response,
    format_task_checkpoint_response,
    format_continue_task_response,
    format_enrich_task_response,
    format_project_bootstrap_response,
    format_project_workflow_response,
    format_project_workflow_submit_response,
    format_remote_snapshot_plan_response,
    format_remote_snapshot_sync_response,
    format_project_readiness_response,
    format_storage_trust_response,
    format_set_canonical_status_response,
    build_load_instruction_layer_payload,
    build_list_instruction_layers_payload,
    format_load_instruction_layer_response,
    format_list_instruction_layers_response,
    sync_tool_definitions,
    tool_definition,
)
from app.services.operational_instincts_service import (
    get_active_operational_instincts,
    render_onboarding_instincts_block,
)
from app.services.mcp_tool_registry import get_tool_stage, observe_tool_use, record_tool_feedback, tool_feedback_expected
from app.services.replay_completeness_service import build_replay_drill_decision, build_token_budget, evaluate_execution_readiness, evaluate_replay_completeness

router = APIRouter(prefix="/mcp")
discovery_router = APIRouter()

import time

# Active SSE sessions: session_id → asyncio.Queue of response dicts
# Queues are in-process (tied to SSE stream) — cannot be stored externally.
_SESSIONS: dict[str, asyncio.Queue] = {}

_SSE_QUEUE_MAXSIZE = 200
_CLEANUP_INTERVAL_S = 60  # 1 minute
_cleanup_task = None
_MAX_SESSION_TOOLS = 120
_MAX_SESSION_QUERIES = 40
_MAX_SESSION_SKILLS = 30
_MAX_SESSION_DIALOGUE_SNIPPETS = 24
_DIALOGUE_TEXT_FIELDS = (
    "query",
    "task",
    "task_description",
    "content",
    "description",
    "message",
    "reason",
    "observation",
    "why_now",
    "why_it_matters",
    "summary",
    "request",
    "prompt",
)

_TOOL_FAMILY_SPECS: dict[str, dict[str, Any]] = {
    "intent_routing": {
        "title": "Intent routing",
        "description": "Canonical first-stage routing for agent intents. Use this before you guess at a tool or endpoint.",
        "entrypoints": ["normalize_mcp_intent", "tool_recommend", "list_tool_families"],
        "keywords": [
            "intent",
            "route",
            "routing",
            "plan",
            "normalize",
            "canonical",
            "surface",
            "mcp",
            "маршрут",
            "намер",
        ],
        "preferred_tools": ["normalize_mcp_intent", "tool_recommend", "list_tool_families", "tool_family_tools", "tool_explain"],
    },
    "tool_discovery": {
        "title": "Tool discovery",
        "description": "Short index for the MCP catalog. Use this family first when the toolset is too large to load in full.",
        "entrypoints": ["list_tool_families", "tool_family_tools", "tool_recommend", "tool_explain", "tool_feedback"],
        "keywords": [
            "tool",
            "tools",
            "mcp",
            "catalog",
            "catalogue",
            "family",
            "families",
            "discovery",
            "инструмент",
            "инструменты",
            "каталог",
            "семейств",
        ],
        "preferred_tools": ["list_tool_families", "tool_recommend", "tool_family_tools", "tool_explain", "tool_feedback"],
    },
    "project_knowledge": {
        "title": "Project knowledge & artifact lifecycle",
        "description": "Unified discovery for tasks, improvements, readiness, canonical knowledge, lifecycle checkpoints, reopen/resume flows, and lifecycle changes.",
        "entrypoints": ["project_workflow", "continue_task", "list_open_tasks", "reopen_task", "record_task_checkpoint", "report_task_checkpoint", "list_artifacts", "enrich_task_with_context", "review_improvement"],
        "keywords": [
            "task",
            "tasks",
            "improvement",
            "artifact",
            "project",
            "project knowledge",
            "readiness",
            "bootstrap",
            "canonical",
            "law",
            "docs",
            "context",
            "knowledge",
            "resolve",
            "reopen",
            "resume",
            "reactivate",
            "snapshot",
            "status",
            "filter",
            "search",
            "задач",
            "улучш",
            "артефакт",
            "проект",
            "контекст",
            "знани",
            "правил",
            "док",
        ],
        "preferred_tools": [
            "project_workflow",
            "project_workflow_submit",
            "continue_task",
            "reopen_task",
            "list_open_tasks",
            "list_artifacts",
            "enrich_task_with_context",
            "review_improvement",
            "record_task_checkpoint",
            "report_task_checkpoint",
            "get_artifact",
            "resolve_artifact",
            "reopen_artifact",
            "search_project_knowledge",
            "get_project_readiness",
            "get_project_bootstrap_checklist",
            "plan_remote_snapshot",
            "sync_remote_snapshot",
            "get_storage_trust_status",
            "set_canonical_status",
            "merge_canonicals",
        ],
    },
    "skills_learning": {
        "title": "Skills & learning",
        "description": "Skill marketplace search, candidate review, and learning outcome management.",
        "entrypoints": ["get_onboarding", "list_learning_candidates", "skill_search"],
        "keywords": [
            "skill",
            "skills",
            "learning",
            "candidate",
            "onboarding",
            "approve",
            "defer",
            "reject",
            "навык",
            "обуч",
            "кандидат",
            "онбординг",
        ],
        "preferred_tools": [
            "get_onboarding",
            "list_learning_candidates",
            "skill_search",
            "approve_learning_candidate",
            "defer_learning_candidate",
            "reject_learning_candidate",
            "skill_publish",
            "skill_install",
            "record_outcome",
        ],
    },
    "coordination": {
        "title": "Agent coordination",
        "description": "Message passing and status management between agents.",
        "entrypoints": ["pickup_coordination_messages", "list_coordination_messages", "send_coordination_message"],
        "keywords": [
            "coordination",
            "message",
            "mailbox",
            "thread",
            "agent",
            "handoff",
            "сообщен",
            "координац",
        ],
        "preferred_tools": [
            "send_coordination_message",
            "pickup_coordination_messages",
            "list_coordination_messages",
            "update_coordination_message_status",
        ],
    },
    "handoff_packets": {
        "title": "Handoff packets",
        "description": "Create, route, refresh, and resume bounded handoff packets for multi-agent work.",
        "entrypoints": ["handoff_task", "decompose_task_packet", "list_handoffs"],
        "keywords": [
            "handoff",
            "packet",
            "route",
            "resume",
            "pickup",
            "background",
            "packet",
            "packets",
            "decompose",
            "перенос",
            "пакет",
        ],
        "preferred_tools": [
            "handoff_task",
            "pickup_handoff",
            "list_pending_handoff_labels",
            "list_handoffs",
            "handoff_workspace_summary",
            "decompose_task_packet",
            "create_task_packets",
            "route_task_packet_execution",
            "dispatch_background_task_packet",
            "reconcile_background_task_packet",
            "expand_handoff_refs",
            "refresh_handoff_context",
            "update_handoff_status",
            "resume_handoff",
        ],
    },
    "instruction_layers": {
        "title": "Instruction layers",
        "description": "Layered instruction access for compact defaults and deeper on-demand detail.",
        "entrypoints": ["list_instruction_layers", "load_instruction_layer"],
        "keywords": [
            "instruction",
            "layer",
            "L2",
            "L3",
            "L4",
            "reference",
            "reference",
            "troubleshooting",
            "инструкц",
            "слой",
        ],
        "preferred_tools": ["list_instruction_layers", "load_instruction_layer"],
    },
    "memory_operations": {
        "title": "Memory operations",
        "description": "Read, ingest, and inspect memories and ingestion flows.",
        "entrypoints": ["memory_search", "memory_context", "ingest_file"],
        "keywords": [
            "memory",
            "memories",
            "ingest",
            "search",
            "context",
            "stats",
            "history",
            "памят",
            "воспомин",
            "ингест",
            "поиск",
        ],
        "preferred_tools": [
            "memory_search",
            "memory_context",
            "memory_stats",
            "memory_health",
            "ingest_file",
            "ingest_dir",
        ],
    },
    "system_observability": {
        "title": "System observability",
        "description": "Health, status, and diagnostic surfaces for the runtime.",
        "entrypoints": ["memory_health", "system_info", "get_task_status"],
        "keywords": [
            "health",
            "system",
            "status",
            "diagnostic",
            "task status",
            "healthcheck",
            "observability",
            "health",
            "диагнос",
            "состояни",
        ],
        "preferred_tools": ["memory_health", "system_info", "get_task_status"],
    },
    "model_routing": {
        "title": "Model routing",
        "description": "Model availability, quota, and rate-limit handling.",
        "entrypoints": ["model_available", "report_limit_hit"],
        "keywords": [
            "model",
            "quota",
            "rate limit",
            "429",
            "route",
            "routing",
            "model",
            "лимит",
            "модель",
        ],
        "preferred_tools": ["model_available", "report_limit_hit"],
    },
    "onboarding": {
        "title": "Onboarding & session feedback",
        "description": "Session bootstrap, outcome reporting, and local reinforcement.",
        "entrypoints": ["get_onboarding", "record_outcome"],
        "keywords": [
            "onboarding",
            "session",
            "outcome",
            "feedback",
            "bootstrap",
            "onboard",
            "онбординг",
            "сесс",
            "результат",
        ],
        "preferred_tools": ["get_onboarding", "record_outcome"],
    },
    "general": {
        "title": "General / uncategorized",
        "description": "Fallback bucket for tools that do not need a dedicated family yet.",
        "entrypoints": [],
        "keywords": [],
        "preferred_tools": [],
    },
}


def _find_tool_definition(tool_name: str) -> dict[str, Any] | None:
    for tool in TOOLS:
        if tool.get("name") == tool_name:
            return tool
    return None


def _infer_tool_family(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    if not name:
        return "general"
    if name in {"normalize_mcp_intent", "list_tool_families", "tool_family_tools", "tool_recommend", "tool_explain", "tool_feedback"}:
        if name == "normalize_mcp_intent":
            return "intent_routing"
        return "tool_discovery"
    if name in {
        "list_artifacts",
        "list_open_tasks",
        "continue_task",
        "reopen_task",
        "get_artifact",
        "resolve_artifact",
        "reopen_artifact",
        "review_improvement",
        "enrich_task_with_context",
        "project_workflow",
        "project_workflow_submit",
        "get_project_readiness",
        "get_project_bootstrap_checklist",
        "plan_remote_snapshot",
        "sync_remote_snapshot",
        "get_storage_trust_status",
        "search_project_knowledge",
        "record_task_checkpoint",
        "report_task_checkpoint",
        "set_canonical_status",
        "merge_canonicals",
    }:
        return "project_knowledge"
    if name.startswith("skill_") or name in {
        "list_learning_candidates",
        "approve_learning_candidate",
        "defer_learning_candidate",
        "reject_learning_candidate",
    }:
        return "skills_learning"
    if "coordination" in name:
        return "coordination"
    if "handoff" in name or "packet" in name or name in {"pickup_handoff", "list_pending_handoff_labels"}:
        return "handoff_packets"
    if "instruction" in name:
        return "instruction_layers"
    if name.startswith("memory_") or name in {"ingest_file", "ingest_dir"}:
        return "memory_operations"
    if name in {"model_available", "report_limit_hit"}:
        return "model_routing"
    if name in {"get_onboarding", "record_outcome"}:
        return "onboarding"
    if name in {"memory_health", "system_info", "get_task_status"}:
        return "system_observability"
    return "general"


def _tool_catalog() -> list[dict[str, Any]]:
    return [tool for tool in TOOLS if isinstance(tool, dict) and tool.get("name")]


def _family_tools(family: str) -> list[dict[str, Any]]:
    return [tool for tool in _tool_catalog() if _infer_tool_family(str(tool.get("name"))) == family]


def _family_spec(family: str) -> dict[str, Any]:
    return _TOOL_FAMILY_SPECS.get(family) or {
        "title": family,
        "description": "Unclassified or fallback tool family.",
        "entrypoints": [],
        "keywords": [],
        "preferred_tools": [],
    }


def _family_recommendation_score(task: str, family: str) -> int:
    spec = _family_spec(family)
    text = task.casefold()
    score = 0
    for keyword in spec.get("keywords", []):
        token = str(keyword).casefold().strip()
        if token and token in text:
            score += 2 if len(token) > 4 else 1
    if family == "tool_discovery" and any(token in text for token in ("tool", "tools", "mcp", "инструмент", "каталог")):
        score += 3
    return score


def _tool_lifecycle_annotations(tool_name: str) -> dict[str, Any]:
    stage = get_tool_stage(str(tool_name or "").strip())
    return {
        "stage": stage,
        "feedback_expected": stage == "testing",
        "follow_up": "tool_feedback" if stage == "testing" else "",
    }


def _annotate_structured_tool_payload(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return data
    enriched = deepcopy(data)
    enriched.update(_tool_lifecycle_annotations(tool_name))
    for key in ("tools", "recommended_tools", "canonical_surface"):
        items = enriched.get(key)
        if not isinstance(items, list):
            continue
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                normalized_items.append(item)
                continue
            item_name = str(item.get("tool") or item.get("name") or "").strip()
            if item_name:
                merged = deepcopy(item)
                merged.update(_tool_lifecycle_annotations(item_name))
                normalized_items.append(merged)
            else:
                normalized_items.append(item)
        enriched[key] = normalized_items
    return enriched


def _build_tool_feedback_envelope(
    *,
    tool_name: str,
    tool_stage: str,
    valence: str,
    worked: bool,
    friction: str,
    suggestion: str,
    task_context: str,
    project_id: str,
    agent_id: str,
    session_id: str,
    missing_fields: list[str],
    feedback_id: int | str,
    assessment: str | None = None,
    scope: str = "",
    what_was_tested: str = "",
    expected_behavior: str = "",
    observed_behavior: str = "",
    next_action: str = "",
    should_promote: bool | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    normalized_stage = str(tool_stage or "testing").strip().lower() or "testing"
    normalized_valence = str(valence or "mixed").strip().lower() or "mixed"
    normalized_friction = str(friction or "").strip()
    normalized_suggestion = str(suggestion or "").strip()
    normalized_task_context = str(task_context or "").strip()
    normalized_scope = str(scope or "").strip()
    normalized_what_was_tested = str(what_was_tested or "").strip()
    normalized_expected_behavior = str(expected_behavior or "").strip()
    normalized_observed_behavior = str(observed_behavior or "").strip()
    normalized_next_action = str(next_action or "").strip()
    normalized_missing_fields = [str(item).strip() for item in missing_fields if str(item).strip()]

    if should_promote is None:
        should_promote = worked and not normalized_friction and not normalized_missing_fields and normalized_stage == "testing"
    if assessment:
        normalized_assessment = assessment
    elif normalized_stage != "testing":
        normalized_assessment = "informational"
    elif not worked:
        normalized_assessment = "needs_redesign" if (normalized_friction or normalized_missing_fields) else "keep_testing"
    elif normalized_friction or normalized_missing_fields:
        normalized_assessment = "keep_testing"
    elif should_promote:
        normalized_assessment = "promote_candidate"
    else:
        normalized_assessment = "keep_testing"

    if not normalized_scope:
        normalized_scope = f"testing {tool_name}"
    if not normalized_what_was_tested:
        normalized_what_was_tested = normalized_task_context or f"Use of {tool_name}"
    if not normalized_expected_behavior:
        normalized_expected_behavior = "Tool should complete the requested path and expose the needed affordances clearly."
    if not normalized_observed_behavior:
        normalized_observed_behavior = "Tool completed the path." if worked else "Tool did not complete the requested path."
    if not normalized_next_action:
        normalized_next_action = {
            "promote_candidate": "Broaden usage, keep monitoring, and consider promotion if signal stays clean.",
            "keep_testing": "Tighten affordances or wording, then retest the same path.",
            "needs_redesign": "Redesign the interface or missing fields before retesting.",
            "deprecate": "Deprecate after confirming there is no better canonical path.",
            "informational": "Use this as an observational note; no promotion decision implied.",
        }.get(normalized_assessment, "Retest with clearer expectations.")
    if confidence is None:
        confidence = 0.9 if normalized_assessment == "promote_candidate" else 0.75 if normalized_assessment == "keep_testing" else 0.45

    return {
        "summary": f"Recorded tool feedback for {tool_name}",
        "feedback_id": feedback_id,
        "tool_name": tool_name,
        "tool_stage": normalized_stage,
        "valence": normalized_valence,
        "worked": worked,
        "assessment": normalized_assessment,
        "should_promote": bool(should_promote),
        "confidence": round(float(confidence), 2),
        "scope": normalized_scope,
        "what_was_tested": normalized_what_was_tested,
        "expected_behavior": normalized_expected_behavior,
        "observed_behavior": normalized_observed_behavior,
        "friction": normalized_friction,
        "suggestion": normalized_suggestion,
        "next_action": normalized_next_action,
        "missing_fields": normalized_missing_fields,
        "task_context": normalized_task_context,
        "project_id": str(project_id or "").strip(),
        "agent_id": str(agent_id or "").strip(),
        "session_id": str(session_id or "").strip(),
    }


def _recommend_family_order(task: str) -> list[str]:
    families = [key for key in _TOOL_FAMILY_SPECS.keys()]
    scored = [
        (family, _family_recommendation_score(task, family), spec.get("title", family))
        for family, spec in ((family, _family_spec(family)) for family in families)
    ]
    scored.sort(key=lambda item: (-item[1], item[2]))
    ordered = [family for family, score, _ in scored if score > 0]
    if "tool_discovery" not in ordered:
        ordered.insert(0, "tool_discovery")
    return ordered


def _build_tool_families_payload(*, include_compatibility_note: bool = True) -> dict[str, Any]:
    families: list[dict[str, Any]] = []
    for family in _TOOL_FAMILY_SPECS:
        tools = _family_tools(family)
        spec = _family_spec(family)
        testing_tools = [tool["name"] for tool in tools if tool_feedback_expected(tool["name"])]
        families.append(
            {
                "family": family,
                "title": spec.get("title", family),
                "description": spec.get("description", ""),
                "tool_count": len(tools),
                "testing_tool_count": len(testing_tools),
                "entrypoints": [name for name in spec.get("entrypoints", []) if _find_tool_definition(name)],
                "sample_tools": [tool["name"] for tool in tools[:5]],
                "testing_tools": testing_tools[:5],
            }
        )
    families.sort(key=lambda item: (-item["tool_count"], item["title"]))
    return {
        "families": families,
        "default_path": ["list_tool_families", "tool_family_tools", "tool_recommend"],
        "compatibility_note": (
            "The full flat tool catalog remains available for compatibility and debugging."
            if include_compatibility_note
            else ""
        ),
    }


def _build_family_tools_payload(family: str, depth: str = "brief", limit: int = 12) -> dict[str, Any]:
    tools = _family_tools(family)[: max(1, min(50, int(limit or 12)))]
    items: list[dict[str, Any]] = []
    for tool in tools:
        item: dict[str, Any] = {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "family": _infer_tool_family(str(tool.get("name"))),
        }
        if depth == "full":
            item["inputSchema"] = deepcopy(tool.get("inputSchema") or {})
        items.append(item)
    spec = _family_spec(family)
    return {
        "family": family,
        "title": spec.get("title", family),
        "description": spec.get("description", ""),
        "depth": depth,
        "tool_count": len(_family_tools(family)),
        "tools": items,
    }


def _build_tool_explanation(tool_name: str, task_context: str = "") -> dict[str, Any]:
    tool = _find_tool_definition(tool_name)
    family = _infer_tool_family(tool_name)
    spec = _family_spec(family)
    stage = get_tool_stage(tool_name)
    if not tool:
        return {
            "tool_name": tool_name,
            "family": family,
            "found": False,
            "message": "Tool not found in the current catalog.",
            "stage": stage,
            "feedback_expected": stage == "testing",
            "related_tools": [name for name in spec.get("preferred_tools", [])[:4]],
        }

    schema = deepcopy(tool.get("inputSchema") or {})
    properties = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    optional = [name for name in properties if name not in required]
    explanation = {
        "tool_name": tool_name,
        "family": family,
        "title": spec.get("title", family),
        "description": tool.get("description", ""),
        "task_context": task_context,
        "when_to_use": spec.get("description", ""),
        "required_args": required,
        "optional_args": optional,
        "input_schema": schema,
        "related_tools": [name for name in spec.get("preferred_tools", []) if name != tool_name][:5],
        "common_pitfalls": [],
    }

    if family == "project_knowledge":
        explanation["common_pitfalls"] = [
            "Use list_open_tasks for open work items before falling back to broader list_artifacts.",
            "Prefer the unified artifact surface over specialized list endpoints.",
            "Record planning and handoff checkpoints with report_task_checkpoint so interruptions do not lose the task.",
        ]
    elif family == "tool_discovery":
        explanation["common_pitfalls"] = [
            "Do not load the flat tool catalog unless you really need compatibility or debugging.",
            "Use tool_family_tools after list_tool_families when you only need one area.",
        ]
    elif family == "handoff_packets":
        explanation["common_pitfalls"] = [
            "Keep packets bounded and route them before dispatching background work.",
        ]
    elif family == "memory_operations":
        explanation["common_pitfalls"] = [
            "Avoid raw file or database inspection when a memory surface can answer the question.",
        ]
    if stage == "testing":
        explanation["common_pitfalls"].append(
            "After using this tool, call tool_feedback with a short worked/blocked report."
        )
    return explanation


def _tool_input_schema(tool_name: str) -> dict[str, Any]:
    tool = _find_tool_definition(tool_name)
    if not tool:
        return {}
    return deepcopy(tool.get("inputSchema") or {})


def _tool_example_payload(tool_name: str, *, intent: str, project_id: str = "") -> dict[str, Any]:
    name = str(tool_name or "").strip()
    project_id = str(project_id or "").strip()
    if name == "reopen_task":
        payload = {"task_id": "<task_id>", "status": "active", "reason": intent[:120] or "reopen_task", "acted_by": "user", "source": "mcp"}
        if project_id:
            payload["project"] = project_id
        return payload
    if name == "list_open_tasks":
        payload = {"project": project_id or "supermemory", "limit": 50}
        return payload
    if name == "report_task_checkpoint":
        return {
            "project": project_id or "supermemory",
            "task_id": "<task_id>",
            "stage": "planning",
            "summary": intent[:160] or "Record task progress",
            "next_step": "Resume from the latest checkpoint.",
            "reason": "normalize_mcp_intent",
            "acted_by": "user",
            "source": "mcp",
        }
    if name == "enrich_task_with_context":
        return {"project_id": project_id or "supermemory", "task": intent[:240], "max_components": 3}
    if name == "resolve_artifact":
        return {"artifact_key": "task:supermemory:<local_id>", "acted_by": "user", "action_source": "normalize_mcp_intent", "reason": intent[:120]}
    if name == "reopen_artifact":
        return {"artifact_key": "task:supermemory:<local_id>", "project": project_id or "supermemory", "status": "active", "reason": "normalize_mcp_intent", "acted_by": "user", "source": "mcp"}
    if name == "list_tool_families":
        return {}
    if name == "tool_recommend":
        return {"task": intent[:240], "project_id": project_id or "", "top_n": 3}
    return {}


def _normalize_mcp_intent(intent: str, *, project_id: str = "", top_n: int = 3) -> dict[str, Any]:
    text = str(intent or "").strip()
    clean_project = str(project_id or "").strip()
    top_n = max(1, min(5, int(top_n or 3)))
    lowered = text.casefold()
    task_id_match = re.search(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", text)
    extracted_task_id = task_id_match.group(0) if task_id_match else ""

    if any(term in lowered for term in ("resume task", "reopen task", "reactivate task", "resume", "reopen", "reactivate", "restore task")):
        resolved_tool = "reopen_task"
        resolved_family = "project_knowledge"
        confidence = 0.96
        rationale = "Task resume intent maps directly to reopen_task."
    elif any(term in lowered for term in ("open tasks", "open work", "list open", "find tasks", "show tasks", "task list")):
        resolved_tool = "list_open_tasks"
        resolved_family = "project_knowledge"
        confidence = 0.88
        rationale = "Open work request maps to list_open_tasks."
    elif any(term in lowered for term in ("checkpoint", "stage", "handoff", "blocker", "progress")):
        resolved_tool = "report_task_checkpoint"
        resolved_family = "project_knowledge"
        confidence = 0.82
        rationale = "Lifecycle/progress intent maps to report_task_checkpoint."
    elif any(term in lowered for term in ("context", "enrich", "why", "what do i need", "project context")):
        resolved_tool = "enrich_task_with_context"
        resolved_family = "project_knowledge"
        confidence = 0.79
        rationale = "Context-building intent maps to enrich_task_with_context."
    elif any(term in lowered for term in ("artifact", "resolve", "reopen artifact", "unified artifact")):
        if "reopen" in lowered:
            resolved_tool = "reopen_artifact"
            rationale = "Artifact reopen intent maps to reopen_artifact."
        else:
            resolved_tool = "resolve_artifact"
            rationale = "Artifact resolution intent maps to resolve_artifact."
        resolved_family = "project_knowledge"
        confidence = 0.75
    else:
        resolved_tool = "tool_recommend"
        resolved_family = "intent_routing"
        confidence = 0.65
        rationale = "Intent is ambiguous; use tool_recommend for a guided next step."

    tool_schema = _tool_input_schema(resolved_tool)
    required = list(tool_schema.get("required") or [])
    properties = tool_schema.get("properties") or {}
    optional = [name for name in properties if name not in required]
    example_payload = _tool_example_payload(resolved_tool, intent=text, project_id=clean_project)
    if extracted_task_id and example_payload.get("task_id") == "<task_id>":
        example_payload["task_id"] = extracted_task_id
    missing_fields = [field for field in required if field not in example_payload or example_payload.get(field) in {"", None, "<task_id>"}]
    cache_version = "mcp-intent-v1"
    cache_material = "|".join([cache_version, resolved_tool, clean_project, lowered])
    cache_key = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()[:16]
    canonical_surface = [
        {
            "tool": "normalize_mcp_intent",
            "family": "intent_routing",
            "why": "First-stage route normalization for agent intent.",
        },
        {
            "tool": "tool_recommend",
            "family": "intent_routing",
            "why": "Fallback when the normalized route remains ambiguous.",
        },
        {
            "tool": "list_tool_families",
            "family": "tool_discovery",
            "why": "Browse the compact catalog only when needed.",
        },
    ]
    if resolved_tool != "tool_recommend":
        canonical_surface.append(
            {
                "tool": resolved_tool,
                "family": resolved_family,
                "why": "Submit the normalized request using the resolved tool.",
            }
        )

    return {
        "intent": text,
        "project_id": clean_project,
        "resolved_family": resolved_family,
        "resolved_tool": resolved_tool,
        "submit_to": resolved_tool,
        "submit_method": "mcp_tool",
        "confidence": confidence,
        "rationale": rationale,
        "required_fields": required,
        "optional_fields": optional,
        "missing_fields": missing_fields,
        "example_payload": example_payload,
        "cache": {
            "key": cache_key,
            "version": cache_version,
            "ttl_seconds": 900,
        },
        "ready_to_execute": not missing_fields and resolved_tool != "tool_recommend",
        "canonical_surface": canonical_surface,
        "next_step": "Call the submit_to tool with the example_payload, filling any missing fields." if resolved_tool != "tool_recommend" else "Call tool_recommend to get a narrower route.",
        "alternatives": [item["tool"] for item in canonical_surface if item["tool"] not in {resolved_tool, "normalize_mcp_intent"}][:top_n],
    }


def _build_tool_recommendation(task: str, project_id: str = "", top_n: int = 3) -> dict[str, Any]:
    project_id = str(project_id or "").strip()
    top_n = max(1, min(5, int(top_n or 3)))
    task_text = str(task or "").strip()
    families = _recommend_family_order(task_text)
    if not families:
        families = ["tool_discovery"]

    recommended_tools: list[dict[str, Any]] = []
    for family in families[:top_n]:
        spec = _family_spec(family)
        preferred = [name for name in spec.get("preferred_tools", []) if _find_tool_definition(name)]
        lowered = task_text.casefold()
        if family == "project_knowledge" and any(term in lowered for term in ("resume task", "reopen task", "reactivate task", "resume", "reopen", "reactivate", "restore task")):
            reopen_first = next((name for name in preferred if name == "reopen_task"), "")
            if reopen_first:
                preferred = [reopen_first] + [name for name in preferred if name != reopen_first]
        if not preferred:
            preferred = [tool["name"] for tool in _family_tools(family)]
        if not preferred:
            continue
        tool_name = preferred[0]
        recommended_tools.append(
            {
                "tool": tool_name,
                "family": family,
                "reason": f"Best match for the task shape based on the {spec.get('title', family)} family.",
            }
        )

    discovery_hint = "Start with list_tool_families when the task does not clearly map to one family."
    if project_id and any(term in task_text.casefold() for term in ("task", "improvement", "artifact", "context", "readiness", "project", "зада", "улучш", "проект")):
        discovery_hint = "For project-local work, prefer the unified project_knowledge family after the family index."

    lowered = task_text.casefold()
    canonical_surface = [
        {
            "tool": "normalize_mcp_intent",
            "family": "intent_routing",
            "why": "Use first when the agent needs a canonical route instead of guessing tools.",
        },
        {
            "tool": "list_tool_families",
            "family": "tool_discovery",
            "why": "Use when the agent is unsure which family to enter first.",
        },
        {
            "tool": "tool_recommend",
            "family": "tool_discovery",
            "why": "Use when the agent wants the system to narrow the next call automatically.",
        },
    ]
    if any(term in lowered for term in ("resume task", "reopen task", "reactivate task", "resume", "reopen", "reactivate", "restore task")):
        canonical_surface.extend([
            {
                "tool": "reopen_task",
                "family": "project_knowledge",
                "why": "Use to resume an existing task without guessing internal endpoints.",
            },
            {
                "tool": "report_task_checkpoint",
                "family": "project_knowledge",
                "why": "Use to persist the current stage and make the task recoverable.",
            },
        ])
    else:
        canonical_surface.extend([
            {
                "tool": "list_open_tasks",
                "family": "project_knowledge",
                "why": "Use to inspect open work items without remembering filters.",
            },
            {
                "tool": "enrich_task_with_context",
                "family": "project_knowledge",
                "why": "Use to turn a task into a compact context bundle before choosing deeper tools.",
            },
            {
                "tool": "report_task_checkpoint",
                "family": "project_knowledge",
                "why": "Use to save task progress at planning and stage transitions.",
            },
            {
                "tool": "reopen_task",
                "family": "project_knowledge",
                "why": "Use when an existing task needs to be made active again.",
            },
        ])

    return {
        "task": task_text,
        "project_id": project_id,
        "canonical_surface": canonical_surface,
        "recommended_families": [
            {
                "family": family,
                "title": _family_spec(family).get("title", family),
                "description": _family_spec(family).get("description", ""),
                "tool_count": len(_family_tools(family)),
            }
            for family in families[:top_n]
        ],
        "recommended_tools": recommended_tools[:top_n],
        "fallback": {
            "tool": "list_tool_families",
            "reason": discovery_hint,
        },
    }


def _normalize_dialogue_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) < 24:
        return ""
    if len(re.findall(r"[A-Za-z\u0400-\u04FF]", cleaned)) < 12:
        return ""
    return cleaned[:480]


def _extract_dialogue_snippets(args: dict[str, Any] | None) -> list[str]:
    if not isinstance(args, dict):
        return []

    snippets: list[str] = []
    seen: set[str] = set()

    def _push(raw: Any) -> None:
        text = _normalize_dialogue_text(raw)
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        snippets.append(text)

    for key in _DIALOGUE_TEXT_FIELDS:
        _push(args.get(key))

    memories = args.get("memories")
    if isinstance(memories, list):
        for item in memories[:5]:
            if isinstance(item, dict):
                _push(item.get("content"))
                _push(item.get("description"))

    packets = args.get("packets")
    if isinstance(packets, list):
        for item in packets[:5]:
            if isinstance(item, dict):
                _push(item.get("task_description"))
                _push(item.get("definition_of_done"))

    return snippets[:8]


def _build_dialogue_transcript(snippets: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in snippets[-12:]:
        text = _normalize_dialogue_text(item.get("text"))
        if not text:
            continue
        tool = str(item.get("tool") or "").strip()
        if tool:
            lines.append(f"USER: ({tool}) {text}")
        else:
            lines.append(f"USER: {text}")
    return "\n".join(lines)[-4000:]


async def _touch_session(session_id: str) -> None:
    from app.services.mcp_session_store import get_session_store
    await get_session_store().touch(session_id)


async def _evict_expired_sessions() -> int:
    from app.services.mcp_session_store import get_session_store
    return await get_session_store().evict_expired()


def _ensure_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _loop():
        while True:
            try:
                await asyncio.sleep(_CLEANUP_INTERVAL_S)
                await _evict_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    _cleanup_task = loop.create_task(_loop())


async def _queue_put(queue: asyncio.Queue, msg: dict) -> None:
    try:
        if queue.full():
            try:
                queue.get_nowait()
            except Exception:
                pass
        queue.put_nowait(msg)
    except Exception:
        await queue.put(msg)


async def _session_observe(session_id: str | None, tool_name: str, extra: dict | None = None) -> None:
    """Record a tool call in the session context for passive tracking."""
    if not session_id:
        return
    from app.services.mcp_session_store import get_session_store
    store = get_session_store()
    context = await store.get_context(session_id)
    if not context:
        return

    tools_called = list(context.get("tools_called") or [])
    tools_called.append({"tool": tool_name, "ts": time.time()})
    context["tools_called"] = tools_called[-_MAX_SESSION_TOOLS:]

    if tool_name in ("memory_search", "memory_context") and extra and extra.get("query"):
        queries = list(context.get("queries") or [])
        query = str(extra.get("query") or "").strip()
        if query:
            queries.append(query[:300])
            context["queries"] = queries[-_MAX_SESSION_QUERIES:]
    elif tool_name in ("skill_search", "skill_install") and extra and extra.get("query"):
        skills = list(context.get("skills_accessed") or [])
        query = str(extra.get("query") or "").strip()
        if query:
            skills.append(query[:200])
            context["skills_accessed"] = skills[-_MAX_SESSION_SKILLS:]

    snippets = _extract_dialogue_snippets(extra)
    if snippets:
        session_snippets = list(context.get("dialogue_snippets") or [])
        session_snippets.extend(
            {"tool": tool_name, "text": snippet, "ts": time.time()}
            for snippet in snippets
        )
        context["dialogue_snippets"] = session_snippets[-_MAX_SESSION_DIALOGUE_SNIPPETS:]

    await store.set_context(session_id, context)


async def _mcp_live_observe(tool_name: str, args: dict, api_base: str) -> None:
    """
    MCP-agnostic server-side observer. Fires for every MCP client (Claude Code,
    Codex, Cline, Cursor, …) without requiring client-side hooks.

    - Emits canonical tool_call event for observability
    - Emits user_request event when tool arguments contain meaningful natural language
    - For memory_store / memory_search: updates project activity so decay gate
      doesn't penalise projects that write/search but never call memory_context
    """
    try:
        project = (
            args.get("project_id")
            or args.get("context_project")
            or args.get("project")
            or ""
        )
        agent_id = args.get("agent_id") or "mcp-client"
        dialogue_snippets = _extract_dialogue_snippets(args)

        # Emit lightweight tool_call event (feeds dialogue analyzer + learning store)
        await _post(api_base, "/learning/events", {
            "event_type": "tool_call",
            "agent_id": agent_id,
            "project": project,
            "transport": "mcp",
            "episode_id": "",
            "context_signature": f"project={project};tool={tool_name};transport=mcp",
            "payload": {"tool_name": tool_name},
        })

        if dialogue_snippets:
            await _post(api_base, "/learning/events", {
                "event_type": "user_request",
                "agent_id": agent_id,
                "project": project,
                "transport": "mcp",
                "episode_id": "",
                "context_signature": f"project={project};category=user_request;tool={tool_name};transport=mcp",
                "payload": {
                    "request_type": "tool_intent",
                    "tool_name": tool_name,
                    "request_text": dialogue_snippets[0],
                    "snippet_count": len(dialogue_snippets),
                },
            })

        # For write/search tools: mark project as active so decay gate fires correctly
        # (memory_context already does this via /memories/context route internally)
        if tool_name in ("memory_store", "memory_search", "memory_batch_store") and project:
            from app.dependencies import get_qdrant
            qdrant = get_qdrant()
            await qdrant.mark_used([], project=project)  # empty ids = just update activity ts

    except Exception:
        pass  # Never surface observer errors to MCP client


def _build_handoff_context_refs(enrich_data: dict[str, Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    laws = [str(item.get("id") or "").strip() for item in enrich_data.get("laws") or [] if str(item.get("id") or "").strip()]
    if laws:
        refs["laws"] = laws[:10]
    components = [str(item.get("component_id") or "").strip() for item in enrich_data.get("components") or [] if str(item.get("component_id") or "").strip()]
    if components:
        refs["components"] = components[:10]
    improvements = [str(item.get("id") or "").strip() for item in enrich_data.get("improvements") or [] if str(item.get("id") or "").strip()]
    if improvements:
        refs["improvements"] = improvements[:10]
    runtime_hints = [str(item.get("id") or "").strip() for item in enrich_data.get("runtime_hints") or [] if str(item.get("id") or "").strip()]
    if runtime_hints:
        refs["runtime_hints"] = runtime_hints[:10]
    tasks = [str(item.get("task_id") or "").strip() for item in enrich_data.get("tasks") or [] if str(item.get("task_id") or "").strip()]
    if tasks:
        refs["tasks"] = tasks[:10]
    task_capture_candidates = [str(item.get("artifact_id") or "").strip() for item in enrich_data.get("task_capture_candidates") or [] if str(item.get("artifact_id") or "").strip()]
    if task_capture_candidates:
        refs["task_capture_candidates"] = task_capture_candidates[:10]
    docs_sections = [str(item.get("section_key") or "").strip() for item in enrich_data.get("docs_sections") or [] if str(item.get("section_key") or "").strip()]
    if docs_sections:
        refs["docs_sections"] = docs_sections[:10]
    return refs


def _build_handoff_context_summary(enrich_data: dict[str, Any]) -> str:
    coverage = []
    for key in ("laws", "components", "improvements", "runtime_hints", "tasks", "task_capture_candidates", "docs_sections"):
        count = len(enrich_data.get(key) or [])
        if count:
            coverage.append(f"{key}={count}")
    highlights: list[str] = []
    laws = enrich_data.get("laws") or []
    if laws:
        titles = [str(item.get("title") or "").strip() for item in laws[:2] if str(item.get("title") or "").strip()]
        if titles:
            highlights.append("laws: " + ", ".join(titles))
    components = enrich_data.get("components") or []
    if components:
        names = [str(item.get("name") or item.get("component_id") or "").strip() for item in components[:2] if str(item.get("name") or item.get("component_id") or "").strip()]
        if names:
            highlights.append("components: " + ", ".join(names))
    improvements = enrich_data.get("improvements") or []
    if improvements:
        titles = [str(item.get("title") or "").strip() for item in improvements[:2] if str(item.get("title") or "").strip()]
        if titles:
            highlights.append("improvements: " + ", ".join(titles))
    task_triage = enrich_data.get("task_triage") or {}
    recommended_task_id = str(task_triage.get("recommended_task_id") or "").strip()
    if recommended_task_id:
        highlights.append("next_task: " + recommended_task_id)
    task_capture_candidates = enrich_data.get("task_capture_candidates") or []
    if task_capture_candidates:
        labels = []
        for item in task_capture_candidates[:2]:
            kind = str(item.get("kind") or "draft").strip()
            task_id = str(item.get("task_id") or "").strip()
            labels.append(f"{kind}@{task_id}" if task_id else kind)
        if labels:
            highlights.append("capture_drafts: " + ", ".join(labels))
    parts: list[str] = []
    if coverage:
        parts.append("coverage " + ", ".join(coverage))
    if highlights:
        parts.append("highlights " + " | ".join(highlights))
    if enrich_data.get("code_inspection_recommended"):
        parts.append("code inspection fallback recommended")
    return "; ".join(parts)[:2000]


def _summarize_handoff_ref_counts(refs: dict[str, list[str]]) -> str:
    parts = [f"{key}={len(values)}" for key, values in refs.items() if values]
    return ", ".join(parts)


def _summarize_handoff_bucket_counts(values: dict[str, Any]) -> str:
    return ", ".join(f"{key}={count}" for key, count in values.items() if count)


def _format_handoff_merge_back_guidance(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()[:180]
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "; ".join(items[:3])[:180]
    if isinstance(value, dict):
        priority_keys = ("summary", "action", "reason", "guidance", "target", "next_step")
        parts: list[str] = []
        for key in priority_keys:
            raw = value.get(key)
            if raw not in (None, "", [], {}):
                parts.append(f"{key}={raw}")
        if not parts:
            for key, raw in value.items():
                if raw not in (None, "", [], {}):
                    parts.append(f"{key}={raw}")
                if len(parts) >= 3:
                    break
        return "; ".join(parts)[:180]
    return str(value).strip()[:180]


def _format_handoff_scope(scope: Any) -> str:
    if isinstance(scope, str):
        return scope.strip()
    if isinstance(scope, list):
        values = [str(item).strip() for item in scope if str(item).strip()]
        return ", ".join(values)
    return str(scope).strip()


def _format_handoff_background_payload(payload: Any) -> str:
    if payload in (None, "", [], {}):
        return ""
    if isinstance(payload, str):
        return payload.strip()[:240]
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:240]
    except Exception:
        return str(payload).strip()[:240]


def _append_handoff_background_state(parts: list[str], item: dict[str, Any]) -> None:
    if item.get("background_job_status"):
        parts.append(f"background_job_status={item['background_job_status']}")
    if item.get("dispatched_job_id"):
        parts.append(f"dispatched_job_id={item['dispatched_job_id']}")


def _extract_handoff_field(item: dict[str, Any], field: str) -> Any:
    value = item.get(field)
    if value not in (None, "", []):
        return value
    content = item.get("content") or ""
    prefix = f"{field}:"
    for line in content.splitlines():
        if line.startswith(prefix):
            raw = line[len(prefix):].strip()
            if field == "write_scope":
                return [part.strip() for part in raw.split(",") if part.strip()]
            return raw
    return None


def _sanitize_handoff_content_preview(content: str) -> str:
    filtered: list[str] = []
    skip_prefixes = (
        "project_context_summary:",
        "project_context_refs:",
        "project_context_snapshot:",
        "project_id:",
        "phase:",
        "priority:",
        "definition_of_done:",
        "expected_output_shape:",
        "phase_objective:",
        "owner_agent:",
        "write_scope:",
        "executor_used:",
        "model_used:",
        "execution_mode:",
        "background_job_type:",
        "background_payload:",
        "core_instinct_ids:",
        "supporting_instinct_ids:",
    )
    for line in (content or "").splitlines():
        if any(line.startswith(prefix) for prefix in skip_prefixes):
            continue
        filtered.append(line)
    return "\n".join(filtered)[:800]


def _format_handoff_workspace_summary(data: dict[str, Any]) -> str:
    agent_id = data.get("agent_id") or "unknown"
    statuses = ", ".join(data.get("statuses") or ["all"])
    lines = [f"Workspace handoff summary for '{agent_id}' ({statuses}):"]
    if data.get("handoff_label"):
        lines.append(f"handoff_label: {data['handoff_label']}")
    if data.get("owner_agent"):
        lines.append(f"owner_agent: {data['owner_agent']}")
    if data.get("write_scope"):
        lines.append(f"write_scope: {_format_handoff_scope(data['write_scope'])}")
    lines.append(f"total: {data.get('total', 0)}")
    if data.get("by_status"):
        lines.append("by_status: " + _summarize_handoff_bucket_counts(data["by_status"]))
    if data.get("by_owner_agent"):
        lines.append("by_owner_agent: " + _summarize_handoff_bucket_counts(data["by_owner_agent"]))
    if data.get("by_phase"):
        lines.append("by_phase: " + _summarize_handoff_bucket_counts(data["by_phase"]))
    if data.get("by_execution_mode"):
        lines.append("by_execution_mode: " + _summarize_handoff_bucket_counts(data["by_execution_mode"]))
    if data.get("by_executor_used"):
        lines.append("by_executor_used: " + _summarize_handoff_bucket_counts(data["by_executor_used"]))
    if data.get("merge_back_guidance"):
        lines.append(f"merge_back_guidance: {_format_handoff_merge_back_guidance(data['merge_back_guidance'])}")
    parallel = data.get("parallel_execution") or {}
    if parallel:
        lines.append(
            "parallel_execution: "
            + f"running={int(parallel.get('running_count', 0))}, "
            + f"planned={int(parallel.get('planned_packet_count', 0))}, "
            + f"blocked={int(parallel.get('blocked_count', 0))}, "
            + f"waves={len(parallel.get('waves') or [])}"
        )
        waves = parallel.get("waves") or []
        for wave in waves[:3]:
            lines.append(
                f"- wave {wave.get('wave')}: packets={wave.get('packet_count', 0)} "
                f"write_scope={_format_handoff_scope(wave.get('write_scope_union') or [])}"
            )
        if len(waves) > 3:
            lines.append(f"- ... {len(waves) - 3} more waves")
        blocked = parallel.get("blocked_packets") or []
        if blocked:
            lines.append(f"parallel_blocked_packets: {len(blocked)}")
    recent_packets = data.get("recent_packets") or []
    if recent_packets:
        lines.append("recent_packets:")
        for item in recent_packets:
            parts = []
            task_id = item.get("task_id") or "unknown"
            label = item.get("handoff_label")
            status = item.get("status") or "unknown"
            owner = item.get("owner_agent") or "unassigned"
            phase = item.get("phase") or "unspecified"
            priority = item.get("priority") or "unspecified"
            memory_id = item.get("memory_id") or "unknown"
            executor_used = item.get("executor_used")
            model_used = item.get("model_used")
            parts.append(f"task_id={task_id}")
            if label:
                parts.append(f"label={label}")
            parts.extend(
                [
                    f"status={status}",
                    f"owner_agent={owner}",
                    f"phase={phase}",
                    f"priority={priority}",
                    f"memory_id={memory_id}",
                ]
            )
            if executor_used:
                parts.append(f"executor_used={executor_used}")
            if model_used:
                parts.append(f"model_used={model_used}")
            if item.get("execution_mode"):
                parts.append(f"execution_mode={item['execution_mode']}")
            if item.get("background_job_type"):
                parts.append(f"background_job_type={item['background_job_type']}")
            if item.get("background_payload"):
                parts.append(f"background_payload={_format_handoff_background_payload(item['background_payload'])}")
            _append_handoff_background_state(parts, item)
            if item.get("project_context_ref_counts"):
                parts.append(f"refs={item['project_context_ref_counts']}")
            lines.append("- " + " ".join(parts))
    if data.get("pending_labels"):
        lines.append(f"pending_labels: {len(data['pending_labels'])}")
    return "\n".join(lines)


def _format_handoff_decomposition(data: dict[str, Any]) -> str:
    lines = ["Task packet decomposition:"]
    if data.get("project_id"):
        lines.append(f"project_id: {data['project_id']}")
    lines.append(f"strategy: {data.get('strategy') or 'unknown'}")
    lines.append(f"recommended_packet_count: {data.get('recommended_packet_count', 0)}")
    if data.get("phase"):
        lines.append(f"phase: {data['phase']}")
    if data.get("phase_objective"):
        lines.append(f"phase_objective: {data['phase_objective']}")
    if data.get("why_split"):
        lines.append(f"why_split: {data['why_split']}")
    packets = data.get("packets") or []
    if packets:
        lines.append("packets:")
        for item in packets:
            parts = [f"label={item.get('handoff_label') or 'packet'}"]
            if item.get("owner_agent"):
                parts.append(f"owner_agent={item['owner_agent']}")
            parts.append(f"phase={item.get('phase') or 'unspecified'}")
            parts.append(f"priority={item.get('priority') or 'medium'}")
            if item.get("execution_mode"):
                parts.append(f"execution_mode={item['execution_mode']}")
            if item.get("suggested_execution_tier"):
                parts.append(f"suggested_execution_tier={item['suggested_execution_tier']}")
            if item.get("background_job_type"):
                parts.append(f"background_job_type={item['background_job_type']}")
            if item.get("background_payload"):
                parts.append(f"background_payload={_format_handoff_background_payload(item['background_payload'])}")
            _append_handoff_background_state(parts, item)
            if item.get("model_hint"):
                parts.append(f"model_hint={item['model_hint']}")
            if item.get("write_scope"):
                parts.append(f"write_scope={_format_handoff_scope(item['write_scope'])}")
            if item.get("executor_used"):
                parts.append(f"executor_used={item['executor_used']}")
            if item.get("model_used"):
                parts.append(f"model_used={item['model_used']}")
            lines.append("- " + " ".join(parts))
            if item.get("definition_of_done"):
                lines.append(f"  done: {item['definition_of_done']}")
            if item.get("expected_output_shape"):
                lines.append(f"  output: {item['expected_output_shape']}")
    return "\n".join(lines)


def _format_created_task_packets(data: dict[str, Any]) -> str:
    packets = data.get("created_packets") or data.get("packets") or []
    created_count = data.get("created_count")
    if created_count is None:
        created_count = len(packets)
    lines = [f"Created {created_count} task packet(s)"]
    if data.get("project_id"):
        lines.append(f"project_id: {data['project_id']}")
    if data.get("task_description"):
        lines.append(f"task_description: {data['task_description']}")
    if data.get("reason"):
        lines.append(f"reason: {data['reason']}")
    if data.get("from_model_id"):
        lines.append(f"from_model_id: {data['from_model_id']}")
    if data.get("partial_result"):
        lines.append(f"partial_result: {data['partial_result'][:500]}")
    if data.get("key_facts"):
        lines.append("key_facts: " + ", ".join(str(item) for item in data.get("key_facts") or []))
    for i, packet in enumerate(packets, 1):
        lines.append(f"\n--- Packet {i} ---")
        if packet.get("task_id"):
            lines.append(f"task_id: {packet['task_id']}")
        if packet.get("handoff_label"):
            lines.append(f"handoff_label: {packet['handoff_label']}")
        if packet.get("memory_id"):
            lines.append(f"memory_id: {packet['memory_id']}")
        if packet.get("to_agent"):
            lines.append(f"to: {packet['to_agent']}")
        if packet.get("status"):
            lines.append(f"status: {packet['status']}")
        if packet.get("owner_agent"):
            lines.append(f"owner_agent: {packet['owner_agent']}")
        if packet.get("write_scope"):
            lines.append(f"write_scope: {_format_handoff_scope(packet['write_scope'])}")
        if packet.get("phase"):
            lines.append(f"phase: {packet['phase']}")
        if packet.get("priority"):
            lines.append(f"priority: {packet['priority']}")
        if packet.get("execution_mode"):
            lines.append(f"execution_mode: {packet['execution_mode']}")
        if packet.get("suggested_execution_tier"):
            lines.append(f"suggested_execution_tier: {packet['suggested_execution_tier']}")
        if packet.get("background_job_type"):
            lines.append(f"background_job_type: {packet['background_job_type']}")
        if packet.get("background_payload"):
            lines.append(f"background_payload: {_format_handoff_background_payload(packet['background_payload'])}")
        if packet.get("background_job_status"):
            lines.append(f"background_job_status: {packet['background_job_status']}")
        if packet.get("dispatched_job_id"):
            lines.append(f"dispatched_job_id: {packet['dispatched_job_id']}")
        if packet.get("model_hint"):
            lines.append(f"model_hint: {packet['model_hint']}")
        if packet.get("executor_used"):
            lines.append(f"executor_used: {packet['executor_used']}")
        if packet.get("model_used"):
            lines.append(f"model_used: {packet['model_used']}")
        if packet.get("definition_of_done"):
            lines.append(f"definition_of_done: {packet['definition_of_done']}")
        if packet.get("expected_output_shape"):
            lines.append(f"expected_output_shape: {packet['expected_output_shape']}")
        if packet.get("phase_objective"):
            lines.append(f"phase_objective: {packet['phase_objective']}")
        if packet.get("pickup_instruction"):
            lines.append(f"Instruction: {packet['pickup_instruction']}")
    return "\n".join(lines)


def _format_route_task_packet_execution(data: dict[str, Any]) -> str:
    def _compact_value(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()[:180]
        if isinstance(value, dict):
            keys = (
                "name",
                "id",
                "component",
                "component_id",
                "executor",
                "model_id",
                "executor_used",
                "model_used",
                "tier",
                "score",
                "confidence",
                "reason",
                "summary",
                "description",
            )
            parts: list[str] = []
            for key in keys:
                raw = value.get(key)
                if raw not in (None, "", [], {}):
                    parts.append(f"{key}={raw}")
                if len(parts) >= 4:
                    break
            if not parts:
                for key, raw in value.items():
                    if raw not in (None, "", [], {}):
                        parts.append(f"{key}={raw}")
                    if len(parts) >= 4:
                        break
            return "; ".join(parts)[:180]
        if isinstance(value, list):
            items = [_compact_value(item) for item in value if str(item).strip()]
            return ", ".join(item for item in items if item)[:180]
        return str(value).strip()[:180]

    lines = ["Route execution recommendation:"]
    if data.get("memory_id"):
        lines.append(f"memory_id: {data['memory_id']}")
    packet = data.get("packet") or {}
    if packet:
        summary_parts: list[str] = []
        if packet.get("task_description"):
            summary_parts.append(f"task_description={packet['task_description'][:120]}")
        if packet.get("phase"):
            summary_parts.append(f"phase={packet['phase']}")
        if packet.get("execution_mode"):
            summary_parts.append(f"execution_mode={packet['execution_mode']}")
        if packet.get("suggested_execution_tier"):
            summary_parts.append(f"suggested_execution_tier={packet['suggested_execution_tier']}")
        if packet.get("background_job_type"):
            summary_parts.append(f"background_job_type={packet['background_job_type']}")
        if packet.get("background_payload"):
            summary_parts.append(f"background_payload={_format_handoff_background_payload(packet['background_payload'])}")
        if packet.get("background_job_status"):
            summary_parts.append(f"background_job_status={packet['background_job_status']}")
        if packet.get("dispatched_job_id"):
            summary_parts.append(f"dispatched_job_id={packet['dispatched_job_id']}")
        if packet.get("model_hint"):
            summary_parts.append(f"model_hint={packet['model_hint']}")
        if packet.get("priority"):
            summary_parts.append(f"priority={packet['priority']}")
        if packet.get("owner_agent"):
            summary_parts.append(f"owner_agent={packet['owner_agent']}")
        if packet.get("write_scope"):
            summary_parts.append(f"write_scope={_format_handoff_scope(packet['write_scope'])}")
        if packet.get("executor_used"):
            summary_parts.append(f"executor_used={packet['executor_used']}")
        if packet.get("model_used"):
            summary_parts.append(f"model_used={packet['model_used']}")
        if summary_parts:
            lines.append("packet: " + " ".join(summary_parts))
        if packet.get("definition_of_done"):
            lines.append(f"definition_of_done: {packet['definition_of_done']}")
        if packet.get("expected_output_shape"):
            lines.append(f"expected_output_shape: {packet['expected_output_shape']}")
    if data.get("packet_profile") is not None:
        lines.append(f"packet_profile: {_compact_value(data['packet_profile'])}")
    if data.get("routing_basis") is not None:
        lines.append(f"routing_basis: {_compact_value(data['routing_basis'])}")
    if data.get("eligible_executors") is not None:
        lines.append(f"eligible_executors: {_compact_value(data['eligible_executors'])}")
    if data.get("recommended_executor") is not None:
        lines.append(f"recommended_executor: {_compact_value(data['recommended_executor'])}")
    if data.get("recommended_model") is not None:
        lines.append(f"recommended_model: {_compact_value(data['recommended_model'])}")
    if data.get("recommendation_reason"):
        lines.append(f"recommendation_reason: {data['recommendation_reason']}")
    return "\n".join(lines)


def _format_dispatch_background_task_packet(data: dict[str, Any]) -> str:
    lines = ["Background dispatch queued:"]
    if data.get("memory_id"):
        lines.append(f"memory_id: {data['memory_id']}")
    if data.get("status"):
        lines.append(f"status: {data['status']}")
    if data.get("executor_used"):
        lines.append(f"executor_used: {data['executor_used']}")
    if data.get("model_used"):
        lines.append(f"model_used: {data['model_used']}")
    if data.get("background_job_type"):
        lines.append(f"background_job_type: {data['background_job_type']}")
    if data.get("job_id"):
        lines.append(f"job_id: {data['job_id']}")
    if data.get("background_job_status"):
        lines.append(f"background_job_status: {data['background_job_status']}")
    if data.get("dispatched_job_id"):
        lines.append(f"dispatched_job_id: {data['dispatched_job_id']}")
    if data.get("poll"):
        lines.append(f"poll: {data['poll']}")
    if data.get("recommendation_reason"):
        lines.append(f"recommendation_reason: {data['recommendation_reason']}")
    return "\n".join(lines)


def _format_reconcile_background_task_packet(data: dict[str, Any]) -> str:
    lines = ["Background job reconciled:"]
    if data.get("memory_id"):
        lines.append(f"memory_id: {data['memory_id']}")
    if data.get("status"):
        lines.append(f"status: {data['status']}")
    if data.get("job_id"):
        lines.append(f"job_id: {data['job_id']}")
    if data.get("background_job_status"):
        lines.append(f"background_job_status: {data['background_job_status']}")
    if data.get("background_job_type"):
        lines.append(f"background_job_type: {data['background_job_type']}")
    if data.get("executor_used"):
        lines.append(f"executor_used: {data['executor_used']}")
    if data.get("model_used"):
        lines.append(f"model_used: {data['model_used']}")
    if data.get("result_summary"):
        lines.append(f"result_summary: {data['result_summary']}")
    if data.get("verification_summary"):
        lines.append(f"verification_summary: {data['verification_summary']}")
    if data.get("poll"):
        lines.append(f"poll: {data['poll']}")
    return "\n".join(lines)


def _oauth_metadata(request: Request) -> dict[str, Any]:
    """Return benign OAuth discovery metadata for MCP clients that probe auth first."""
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": None,
        "token_endpoint": None,
        "registration_endpoint": None,
        "grant_types_supported": [],
        "response_types_supported": [],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": [],
    }


@discovery_router.get("/.well-known/oauth-authorization-server")
@discovery_router.get("/.well-known/oauth-authorization-server/mcp/sse")
@discovery_router.get("/mcp/sse/.well-known/oauth-authorization-server")
@discovery_router.get("/.well-known/oauth-protected-resource")
@discovery_router.get("/.well-known/oauth-protected-resource/mcp/sse")
async def oauth_authorization_server(request: Request) -> JSONResponse:
    """
    Return 404 so MCP clients skip OAuth and connect directly with API key.
    Claude Code (new versions) strictly validates OAuth metadata fields and
    rejects null values — 404 is the correct signal for "no OAuth required".
    """
    return JSONResponse(status_code=404, content={"detail": "OAuth not supported"})


# ── Tool definitions (mirrors mcp/server.py TOOLS) ────────────────────────────

TOOLS = [
    {
        "name": "memory_store",
        "description": (
            "Save a new memory to the semantic memory store. "
            "Use this to persist facts, preferences, experiences, tasks, or context "
            "so they can be retrieved later by semantic search."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["content", "agent_id"],
            "properties": {
                "content": {"type": "string", "description": "The memory text to store"},
                "agent_id": {"type": "string", "description": "Identifier of the agent/user who owns this memory"},
                "memory_type": {"type": "string", "enum": ["fact", "preference", "experience", "task", "context"], "default": "fact"},
                "category": {"type": "string", "default": "general"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "source": {"type": "string", "default": "conversation"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "memory_search",
        "description": (
            "Search the semantic memory store using natural language. "
            "Returns the most relevant memories sorted by a composite score "
            "(similarity + importance + recency)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "agent_id": {"type": "string", "description": "Filter by agent ID (optional)"},
                "memory_type": {"type": "string", "enum": ["fact", "preference", "experience", "task", "context"]},
                "category": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5},
                "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0},
                "since_minutes": {"type": "integer", "minimum": 1, "description": "Only memories added within last N minutes"},
            },
        },
    },
    {
        "name": "memory_tree_slice",
        "description": (
            "Search semantic memory using hierarchical tree structure. "
            "Returns knowledge slice from trunk (general) to leaves (specific). "
            "Provides context from general to specific knowledge."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query", "agent_id"],
            "properties": {
                "query": {"type": "string"},
                "agent_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "memory_context",
        "description": (
            "Build a model-ready context bundle from semantic memory search results. "
            "Returns a single text block plus a session_id that can be used with record_memory_outcome."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Natural language query for /memories/context"},
                "agent_id": {"type": "string", "description": "Agent ID (optional)"},
                "memory_type": {"type": "string", "enum": ["fact", "preference", "experience", "task", "context"]},
                "category": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0},
                "since_minutes": {"type": "integer", "minimum": 1, "description": "Only memories added within last N minutes"},
                "max_tokens": {"type": "integer", "minimum": 100, "maximum": 10000, "default": 2000},
                "format": {"type": "string", "enum": ["text", "markdown"], "default": "markdown"},
                "context_project": {"type": "string"},
                "context_file": {"type": "string"},
                "context_task_type": {"type": "string"},
                "session_id": {"type": "string", "description": "Optional episode/session id for outcome linking"},
            },
        },
    },
    {
        "name": "record_memory_outcome",
        "description": (
            "Record success/fail outcome for a /memories/context session and update importance scores "
            "of memories used in that episode."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["success"],
            "properties": {
                "success": {"type": "boolean", "description": "Whether the session outcome was successful"},
                "session_id": {"type": "string", "description": "Episode id returned by memory_context (preferred)"},
                "agent_id": {"type": "string", "description": "Agent identifier (optional)"},
                "project": {"type": "string", "description": "Project name (optional)"},
                "memory_ids": {"type": "array", "items": {"type": "string"}, "default": []},
                "boost": {"type": "number", "minimum": 0, "maximum": 1},
                "penalty": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
    {
        "name": "memory_recent",
        "description": "List memories added recently, sorted by time descending. Use to see what was saved in the last N minutes without needing a search query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 10, "description": "How many minutes back to look"},
                "agent_id": {"type": "string", "description": "Filter by agent ID (optional)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    {
        "name": "memory_get",
        "description": "Retrieve a specific memory by its UUID.",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {"memory_id": {"type": "string", "description": "UUID of the memory"}},
        },
    },
    {
        "name": "memory_delete",
        "description": "Permanently delete a memory by its UUID.",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {"memory_id": {"type": "string", "description": "UUID of the memory to delete"}},
        },
    },
    {
        "name": "memory_batch_store",
        "description": "Store multiple memories in a single request.",
        "inputSchema": {
            "type": "object",
            "required": ["memories"],
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["content", "agent_id"],
                        "properties": {
                            "content": {"type": "string"},
                            "agent_id": {"type": "string"},
                            "memory_type": {"type": "string"},
                            "category": {"type": "string"},
                            "importance_score": {"type": "number"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "minItems": 1,
                    "maxItems": 100,
                }
            },
        },
    },
    {
        "name": "memory_cleanup",
        "description": "Delete old and low-importance memories to free space.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "min_importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.2},
                "max_age_days": {"type": "integer", "minimum": 1, "default": 30},
            },
        },
    },
    {
        "name": "memory_stats",
        "description": "Get statistics about the memory collection (count, status, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "registry_best",
        "description": "Find the best component (local LLM, cloud LLM, skill) for a given task type. Use this to decide whether to handle a task locally or escalate to cloud.",
        "inputSchema": {
            "type": "object",
            "required": ["task_type"],
            "properties": {
                "task_type": {"type": "string", "description": "Task type: layout_fix, log_filter, fact_extraction, code_generation, code_review, text_summarization, skill_tagging, relevance_scoring, memory_extraction, query_expansion, architecture"},
                "exclude": {"type": "string", "description": "Comma-separated components to exclude"},
                "top": {"type": "integer", "default": 3},
            },
        },
    },
    {
        "name": "registry_update",
        "description": "Record a task outcome to update capability scores. Call after every LLM task to improve routing over time.",
        "inputSchema": {
            "type": "object",
            "required": ["component", "task_type", "success"],
            "properties": {
                "component": {"type": "string", "description": "e.g. 'qwen3:1.7b', 'cloud-llm', 'skill:fix-layout'"},
                "task_type": {"type": "string"},
                "success": {"type": "boolean"},
                "description": {"type": "string", "default": ""},
            },
        },
    },
    {
        "name": "registry_components",
        "description": "List all registered components with their capability scores per task type.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "crystallize_solution",
        "description": (
            "Convert a successful cloud LLM solution into a reusable skill (auto-publish). "
            "Call this after solving a task with cloud LLM when the solution is reusable. "
            "qwen3:1.7b will assess reusability, GLM will generate SKILL.md, and it publishes to marketplace automatically. "
            "Future identical tasks will be routed to the skill tier (instant/free). "
            "Use draft_skill instead if you want to review the SKILL.md before publishing."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task", "solution"],
            "properties": {
                "task": {"type": "string", "description": "The task description that was solved"},
                "solution": {"type": "string", "description": "The solution / procedure that worked"},
                "platform": {"type": "string", "default": "claude", "enum": ["claude", "codex", "cursor", "universal"]},
                "force": {"type": "boolean", "default": False, "description": "Crystallize even if reusability score is low"},
            },
        },
    },
    {
        "name": "draft_skill",
        "description": (
            "Three-stage pipeline: local LLM assesses → GLM drafts SKILL.md → YOU review (no auto-publish). "
            "Use this when you want to moderate the skill content before publishing. "
            "Returns draft SKILL.md and reusability score. "
            "After reviewing, call skill_publish with the (possibly edited) content, or discard if not useful."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task", "solution"],
            "properties": {
                "task": {"type": "string", "description": "The task description that was solved"},
                "solution": {"type": "string", "description": "The solution / procedure that worked"},
                "platform": {"type": "string", "default": "claude", "enum": ["claude", "codex", "cursor", "universal"]},
                "force": {"type": "boolean", "default": False, "description": "Generate draft even if reusability score is low"},
            },
        },
    },
    {
        "name": "route_task",
        "description": (
            "Classify a task and get routing recommendation: which component to use (local LLM, cached skill, or cloud LLM). "
            "Use this before expensive cloud LLM calls to check if local can handle it. "
            "After executing, record outcome with track_task."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task"],
            "properties": {
                "task": {"type": "string", "description": "Task description in natural language"},
                "task_type": {"type": "string", "description": "Override auto-classification"},
                "preferred_tier": {"type": "string", "enum": ["local", "cloud", "skill"], "description": "Force a specific tier"},
            },
        },
    },
    {
        "name": "track_task",
        "description": "Record a task execution outcome to the performance tracker. Call after every LLM task to build accurate capability data. If the task was misrouted (wrong task_type), set corrected_task_type to teach the dispatcher.",
        "inputSchema": {
            "type": "object",
            "required": ["component", "task_type", "success"],
            "properties": {
                "component": {"type": "string", "description": "'qwen3:1.7b', 'cloud-llm', 'skill:<name>'"},
                "task_type": {"type": "string"},
                "success": {"type": "boolean"},
                "latency_ms": {"type": "number"},
                "agent_id": {"type": "string"},
                "metadata": {"type": "object"},
                "corrected_task_type": {"type": "string", "description": "Set if the task was misclassified — the actual task type that should have been routed. Ivanov's feedback to Uncle Petya."},
            },
        },
    },
    {
        "name": "tracker_stats",
        "description": "Get aggregate performance statistics: success rates and latencies per component+task_type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "task_type": {"type": "string"},
                "since_hours": {"type": "number", "description": "Limit to last N hours"},
            },
        },
    },
    {
        "name": "report_issue",
        "description": (
            "Report a missing feature, incorrect behavior, or improvement idea encountered while working. "
            "Use this when you hit a limitation or bug in supermemory or any project. "
            "Saved improvements are reviewed during future development sessions."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "description"],
            "properties": {
                "title": {"type": "string", "description": "Short title of the issue or improvement"},
                "description": {"type": "string", "description": "Full description with context, steps to reproduce, expected behavior"},
                "project": {"type": "string", "default": "supermemory", "description": "Which project this applies to"},
                "agent_id": {"type": "string", "default": "llm", "description": "Who is reporting"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7},
                "stage": {
                    "type": "string",
                    "enum": ["proposal", "beta_test", "experimental", "stable", "deprecated"],
                    "default": "proposal",
                    "description": "Optional improvement stage",
                },
                "verdict": {
                    "type": "string",
                    "enum": ["effective", "ineffective"],
                    "description": "Optional quality verdict",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            },
        },
    },
    tool_definition("load_instruction_layer"),
    tool_definition("list_instruction_layers"),
    {
        "name": "list_project_laws",
        "description": "List project laws through MCP so agents can retrieve active project rules without reading repo files directly.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "status": {"type": "string", "enum": ["observed", "proposed", "reviewed", "user_confirmed", "active", "suppressed", "superseded", "archived", "all"], "default": "active"},
                "scope": {"type": "string", "enum": ["project", "family", "domain", "principle", "meta"]},
                "include_promoted": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
        },
    },
    tool_definition("list_learning_candidates"),
    tool_definition("approve_learning_candidate"),
    tool_definition("defer_learning_candidate"),
    tool_definition("reject_learning_candidate"),
    {
        "name": "get_project_law",
        "description": "Retrieve one project law by id.",
        "inputSchema": {
            "type": "object",
            "required": ["law_id"],
            "properties": {
                "law_id": {"type": "string"},
            },
        },
    },
    {
        "name": "improvements_report",
        "description": (
            "Generate a project status report: stats (total/open/resolved, top tags) "
            "and a GLM-written narrative summary with achievements and priorities. "
            "Use when you want a quick structured overview of project health."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "supermemory", "description": "Project name to report on"},
            },
        },
    },
    {
        "name": "knowledge_hierarchy",
        "description": (
            "Inspect canonical knowledge hierarchy grouped by scope. "
            "Returns domain/principle/meta canonicals, totals, and lifecycle counts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic_prefix": {"type": "string", "description": "Optional topic_path prefix filter"},
                "include_suppressed": {"type": "boolean", "default": False},
                "limit_per_scope": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
                "reconcile": {"type": "boolean", "default": False, "description": "Refresh canonical lifecycle before reading"},
            },
        },
    },
    {
        "name": "canonicals_by_scope",
        "description": "List canonical memories for one scope (domain, principle, or meta).",
        "inputSchema": {
            "type": "object",
            "required": ["scope"],
            "properties": {
                "scope": {"type": "string", "enum": ["domain", "principle", "meta"]},
                "topic_prefix": {"type": "string", "description": "Optional topic_path prefix filter"},
                "include_suppressed": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
    },
    tool_definition("set_canonical_status"),
    tool_definition("merge_canonicals"),
    {
        "name": "skill_search",
        "description": (
            "Search the skill marketplace for relevant skills. "
            "Provide a context description to get LLM-filtered results relevant to your current task. "
            "Skills are filtered by domain (e.g. linuxcnc skills won't appear for web dev tasks)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {"type": "string", "description": "Current task/project context for smart filtering (e.g. 'building PyQt5 UI for CNC machine')"},
                "domains": {"type": "string", "description": "Comma-separated domain tags to filter by (e.g. 'web,deploy')"},
                "platform": {"type": "string", "enum": ["claude", "codex", "cursor", "universal"], "description": "Filter by platform"},
                "limit": {"type": "integer", "default": 10},
                "min_relevance": {"type": "number", "default": 0.3},
            },
        },
    },
    {
        "name": "skill_publish",
        "description": "Publish a skill to the shared marketplace. Domain tags are auto-extracted by LLM from the content.",
        "inputSchema": {
            "type": "object",
            "required": ["name", "content"],
            "properties": {
                "name": {"type": "string", "description": "Skill slug name"},
                "content": {"type": "string", "description": "Full SKILL.md content"},
                "platform": {"type": "string", "default": "claude"},
                "agent_id": {"type": "string", "default": "shared"},
                "domain_tags": {"type": "array", "items": {"type": "string"}, "description": "Override auto-detected tags"},
            },
        },
    },
    {
        "name": "skill_install",
        "description": "Get skill content by ID for local installation.",
        "inputSchema": {
            "type": "object",
            "required": ["skill_id"],
            "properties": {
                "skill_id": {"type": "string", "description": "UUID of the skill"},
            },
        },
    },
    tool_definition("get_artifact"),
    tool_definition("list_artifacts"),
    tool_definition("list_open_tasks"),
    tool_definition("normalize_mcp_intent"),
    tool_definition("project_workflow"),
    tool_definition("project_workflow_submit"),
    tool_definition("continue_task"),
    tool_definition("reopen_task"),
    tool_definition("list_tool_families"),
    tool_definition("tool_family_tools"),
    tool_definition("tool_explain"),
    tool_definition("tool_recommend"),
    tool_definition("tool_feedback"),
    tool_definition("record_task_checkpoint"),
    tool_definition("report_task_checkpoint"),
    tool_definition("resolve_artifact"),
    tool_definition("reopen_artifact"),
    {
        "name": "memory_health",
        "description": "Check if the memory server, Qdrant, and Ollama are all reachable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "system_info",
        "description": (
            "Get a full overview of the supermemory system: what components exist, what each does, "
            "live counters (memories, skills, layout terms), active models, and infrastructure status. "
            "Call this when you want to understand what the system can do or need to explain it to the user."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_onboarding",
        "description": (
            "Get a personalized onboarding package based on accumulated experience from previous agents. "
            "Call this at the start of a session to receive relevant skills, behavioral patterns, "
            "domain gaps, and recent context — so you can hit the ground running without knowing the system. "
            "The system learns from each agent's session and passes that knowledge to you."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "description": "Your agent identifier (e.g. 'claude-code', 'codex')"},
                "task_description": {"type": "string", "description": "What you are working on — used to select relevant skills"},
            },
        },
    },
    {
        "name": "record_outcome",
        "description": (
            "Record what was helpful (or not) in your session. "
            "The system uses this to improve onboarding for future agents. "
            "Call at the end of a session or after completing a task."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "success"],
            "properties": {
                "agent_id": {"type": "string"},
                "pack_id": {"type": "string", "description": "Pack ID from get_onboarding (if any)"},
                "skills_helpful": {"type": "array", "items": {"type": "string"}, "description": "Skill IDs that helped"},
                "skills_unused": {"type": "array", "items": {"type": "string"}, "description": "Skill IDs that were irrelevant"},
                "missing_domains": {"type": "array", "items": {"type": "string"}, "description": "Knowledge areas that were missing"},
                "success": {"type": "boolean", "description": "Did the session accomplish its goal?"},
            },
        },
    },
    {
        "name": "ingest_file",
        "description": (
            "Parse a local file (.md, .txt, .rst) into chunks and store each chunk as a memory. "
            "Markdown files are split by headings; text files by paragraphs."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["path", "agent_id"],
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file"},
                "cwd": {"type": "string", "description": "Base directory for resolving a relative path"},
                "agent_id": {"type": "string"},
                "memory_type": {"type": "string", "default": "context"},
                "category": {"type": "string", "default": "document"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "model_available",
        "description": (
            "List available cloud models ranked by remaining quota capacity. "
            "Use before starting a long task to pick the model with most remaining budget. "
            "Optionally filter by task_type to see models capable of specific tasks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string", "description": "Filter by task capability (optional)"},
            },
        },
    },
    {
        "name": "report_limit_hit",
        "description": (
            "Signal that a cloud model hit its rate/quota limit (429 or similar error). "
            "Triggers a cooldown period. Call this automatically when you receive a rate-limit error. "
            "After calling this, use model_available or route_task to find the next available model."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["model_id"],
            "properties": {
                "model_id": {"type": "string", "description": "e.g. 'claude-sonnet', 'gpt-4o'"},
                "error_code": {"type": "string", "description": "HTTP error code or API error code"},
                "error_msg": {"type": "string", "description": "Error message from the API"},
                "retry_after": {"type": "integer", "description": "Cooldown seconds (default 3600)"},
            },
        },
    },
    {
        "name": "handoff_task",
        "description": (
            "Package current task context in supermemory for pickup by another CLI tool. "
            "Use when: (1) current model hit its limit, (2) you want to manually switch to another CLI. "
            "Stores context with status=pending. Returns memory_id and pickup instruction for the target CLI."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["from_agent", "to_agent", "task_description"],
            "properties": {
                "from_agent": {"type": "string", "description": "This CLI: claude-code | codex | cline | gemini-cli"},
                "to_agent": {"type": "string", "description": "Target CLI: claude-code | codex | cline | gemini-cli"},
                "project_id": {"type": "string", "description": "Optional project scope for playbook-aware handoffs"},
                "phase": {"type": "string", "description": "Current task lifecycle phase, e.g. task_framing"},
                "priority": {"type": "string", "description": "Human-readable priority like high, medium, low"},
                "owner_agent": {"type": "string", "description": "Optional agent that owns the write scope for this packet"},
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional bounded areas this packet may modify", "default": []},
                "why_now": {"type": "string", "description": "Why this handoff matters now"},
                "definition_of_done": {"type": "string", "description": "What counts as sufficient completion for this iteration"},
                "expected_output_shape": {"type": "string", "description": "Expected shape of the receiving agent's output"},
                "execution_mode": {
                    "type": "string",
                    "enum": ["max_quality", "balanced", "economy", "strict_economy"],
                    "description": "Execution policy mode that biases cost/quality routing for this packet",
                    "default": "balanced",
                },
                "background_job_type": {"type": "string", "description": "Optional existing background job type for safe queued execution"},
                "background_payload": {"type": "object", "description": "Optional payload for background job dispatch", "default": {}},
                "include_project_context": {"type": "boolean", "description": "When project_id is set, attach a compact enrich-task context snapshot", "default": True},
                "context_max_components": {"type": "integer", "description": "Max components to include in the attached project context snapshot", "default": 3},
                "task_description": {"type": "string", "description": "Full task description to hand off"},
                "partial_result": {"type": "string", "description": "Any partial work done so far"},
                "key_facts": {"type": "array", "items": {"type": "string"}, "description": "Up to 10 key facts the next agent needs"},
                "task_id": {"type": "string", "description": "Task identifier (auto-generated if omitted)"},
                "handoff_label": {"type": "string", "description": "Human-readable label like 'benchmark28' or 'tailcutoff371'"},
                "reason": {"type": "string", "enum": ["manual", "limit_hit"], "default": "manual"},
                "from_model_id": {"type": "string", "description": "Optional cloud model/component that hit a limit, e.g. 'claude-sonnet'"},
            },
        },
    },
    {
        "name": "pickup_handoff",
        "description": (
            "Retrieve pending task handoffs addressed to this CLI agent. "
            "Call this at the start of a new session or when you expect a handoff from another CLI. "
            "Marks retrieved handoffs as picked_up to prevent double-processing."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "description": "This CLI's identity: claude-code | codex | cline | gemini-cli"},
                "handoff_label": {"type": "string", "description": "Optional human-readable label to narrow pickup to one task"},
                "owner_agent": {"type": "string", "description": "Optional agent that owns the write scope for this packet"},
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional bounded areas this packet may modify", "default": []},
                "limit": {"type": "integer", "default": 3, "description": "Max handoffs to retrieve"},
            },
        },
    },
    {
        "name": "list_pending_handoff_labels",
        "description": (
            "List human-readable labels for pending handoffs addressed to this CLI agent. "
            "Use this before pickup when you want to see which named handoffs are waiting."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "description": "This CLI's identity: claude-code | codex | cline | gemini-cli"},
                "limit": {"type": "integer", "default": 20, "description": "Max labels to return"},
            },
        },
    },
    {
        "name": "list_handoffs",
        "description": (
            "List task packets/handoffs for this CLI agent filtered by lifecycle status. "
            "Use this to inspect active, paused, picked_up, closed, or archived packets, not only pending pickup."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "description": "This CLI's identity: claude-code | codex | cline | gemini-cli"},
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Statuses like ['active','paused'] or ['all']",
                    "default": [],
                },
                "handoff_label": {"type": "string", "description": "Optional human-readable label to narrow listing"},
                "owner_agent": {"type": "string", "description": "Optional agent that owns the write scope for this packet"},
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional bounded areas this packet may modify", "default": []},
                "limit": {"type": "integer", "default": 20, "description": "Max packets to return"},
            },
        },
    },
    {
        "name": "handoff_workspace_summary",
        "description": (
            "Return a compact overview of the current handoff workspace for an agent. "
            "Includes total packets, breakdowns by status/owner/phase, and compact recent packet lines."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "description": "This CLI's identity: claude-code | codex | cline | gemini-cli"},
                "statuses": {"type": "array", "items": {"type": "string"}, "description": "Lifecycle statuses to include", "default": []},
                "handoff_label": {"type": "string", "description": "Optional human-readable label to narrow summary"},
                "owner_agent": {"type": "string", "description": "Optional current owner to narrow summary"},
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional required write-scope entries to narrow summary", "default": []},
                "packet_limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "How many recent compact packets to include"},
            },
        },
    },
    {
        "name": "decompose_task_packet",
        "description": (
            "Build a deterministic recommendation for splitting a larger task into bounded task packets. "
            "Returns packet stubs with suggested labels, phases, write scopes, and iteration contracts."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_description"],
            "properties": {
                "project_id": {"type": "string", "description": "Optional project scope for project-aware playbook guidance"},
                "task_description": {"type": "string", "description": "The larger task to split into bounded packets"},
                "handoff_label_prefix": {"type": "string", "description": "Optional label prefix for the suggested packet stubs"},
                "phase": {"type": "string", "description": "Preferred task lifecycle phase for the suggested packets"},
                "priority": {"type": "string", "description": "Priority to copy into the suggested packet stubs"},
                "owner_agent": {"type": "string", "description": "Optional default owner to assign to the suggested packets"},
                "execution_mode": {
                    "type": "string",
                    "enum": ["max_quality", "balanced", "economy", "strict_economy"],
                    "description": "Policy mode that biases decomposition toward quality or economy",
                    "default": "balanced",
                },
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Candidate bounded write scopes or work areas", "default": []},
                "max_packets": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4, "description": "Maximum number of packet stubs to recommend"},
            },
        },
    },
    {
        "name": "create_task_packets",
        "description": (
            "Materialize recommended packet stubs into real task packets. "
            "Use after decomposition when you want to create multiple bounded task packets at once."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["from_agent", "to_agent", "task_description", "packets"],
            "properties": {
                "from_agent": {"type": "string", "description": "Source CLI: claude-code | codex | cline | gemini-cli"},
                "to_agent": {"type": "string", "description": "Target CLI: claude-code | codex | cline | gemini-cli"},
                "project_id": {"type": "string", "description": "Optional project scope for project-aware packet creation"},
                "task_description": {"type": "string", "description": "Fallback task description used when a packet stub does not override it"},
                "execution_mode": {
                    "type": "string",
                    "enum": ["max_quality", "balanced", "economy", "strict_economy"],
                    "description": "Fallback execution mode used when a packet stub does not override it",
                    "default": "balanced",
                },
                "partial_result": {"type": "string", "description": "Optional partial result carried into each created packet"},
                "key_facts": {"type": "array", "items": {"type": "string"}, "description": "Up to 10 key facts carried into each created packet", "default": []},
                "reason": {"type": "string", "enum": ["manual", "limit_hit"], "default": "manual"},
                "from_model_id": {"type": "string", "description": "Optional originating cloud model/component"},
                "agent_id": {"type": "string", "description": "Memory agent_id for Qdrant storage", "default": "handoff"},
                "packets": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "description": "Recommended packet stubs to materialize as real task packets",
                    "items": {
                        "type": "object",
                        "properties": {
                            "handoff_label": {"type": "string", "description": "Human-readable packet label"},
                            "task_description": {"type": "string", "description": "Packet-specific task description"},
                            "owner_agent": {"type": "string", "description": "Optional owner for the created packet"},
                            "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Bounded write scope for the created packet", "default": []},
                            "phase": {"type": "string", "description": "Packet lifecycle phase"},
                            "priority": {"type": "string", "description": "Packet priority"},
                            "execution_mode": {
                                "type": "string",
                                "enum": ["max_quality", "balanced", "economy", "strict_economy"],
                                "description": "Execution mode for the created packet",
                            },
                            "background_job_type": {"type": "string", "description": "Optional background job type for the created packet"},
                            "background_payload": {"type": "object", "description": "Optional payload for background dispatch", "default": {}},
                            "suggested_execution_tier": {"type": "string", "description": "Suggested execution tier for the created packet"},
                            "model_hint": {"type": "string", "description": "Suggested model hint for the created packet"},
                            "why_now": {"type": "string", "description": "Why this packet matters now"},
                            "definition_of_done": {"type": "string", "description": "Completion contract for the packet"},
                            "expected_output_shape": {"type": "string", "description": "Expected output shape for the packet"},
                            "phase_objective": {"type": "string", "description": "Phase objective carried into the packet"},
                            "core_instinct_ids": {"type": "array", "items": {"type": "string"}, "description": "Core instinct IDs for the packet", "default": []},
                            "supporting_instinct_ids": {"type": "array", "items": {"type": "string"}, "description": "Supporting instinct IDs for the packet", "default": []},
                            "project_context_summary": {"type": "string", "description": "Optional compact project context summary"},
                            "project_context_refs": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}, "description": "Optional project context references", "default": {}},
                            "project_context_snapshot": {"type": "string", "description": "Optional project context snapshot"},
                        },
                    },
                },
            },
        },
    },
    {
        "name": "route_task_packet_execution",
        "description": (
            "Route a handoff packet to the best execution target using its packet payload or memory_id. "
            "Returns packet profile, routing basis, eligible executors, and the recommended executor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Optional handoff memory UUID"},
                "packet": {
                    "type": "object",
                    "description": "Optional packet payload to route",
                    "properties": {
                        "task_description": {"type": "string"},
                        "write_scope": {"type": "array", "items": {"type": "string"}, "default": []},
                        "phase": {"type": "string"},
                        "suggested_execution_tier": {"type": "string"},
                        "execution_mode": {
                            "type": "string",
                            "enum": ["max_quality", "balanced", "economy", "strict_economy"],
                        },
                        "background_job_type": {"type": "string"},
                        "background_payload": {"type": "object", "default": {}},
                        "model_hint": {"type": "string"},
                        "definition_of_done": {"type": "string"},
                        "expected_output_shape": {"type": "string"},
                        "phase_objective": {"type": "string"},
                        "priority": {"type": "string"},
                        "owner_agent": {"type": "string"},
                        "why_now": {"type": "string"},
                        "task_id": {"type": "string"},
                        "handoff_label": {"type": "string"},
                        "project_id": {"type": "string"},
                        "from_agent": {"type": "string"},
                        "to_agent": {"type": "string"},
                        "partial_result": {"type": "string"},
                        "key_facts": {"type": "array", "items": {"type": "string"}, "default": []},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    {
        "name": "dispatch_background_task_packet",
        "description": (
            "Dispatch a handoff packet through the background job queue after routing confirms a background executor. "
            "Queues the packet's supported background_job_type and returns the job id and polling location."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "Existing handoff memory UUID to dispatch through the background job queue"},
                "acted_by": {"type": "string", "description": "Actor recording the dispatch action", "default": "user"},
                "reason": {"type": "string", "description": "Dispatch reason or audit note", "default": "background_dispatch"},
            },
        },
    },
    {
        "name": "reconcile_background_task_packet",
        "description": (
            "Reconcile a dispatched background job for a handoff packet after the queue updates its status. "
            "Returns the job state, packet lifecycle state, and any result or verification summary."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "Existing handoff memory UUID with a dispatched background job"},
                "acted_by": {"type": "string", "description": "Actor recording the reconciliation action", "default": "user"},
                "reason": {"type": "string", "description": "Reconciliation reason or audit note", "default": "background_reconcile"},
            },
        },
    },
    {
        "name": "expand_handoff_refs",
        "description": (
            "Resolve referenced project context from a handoff packet on demand. "
            "Use this after pickup when the compact handoff summary is not enough and you need selected laws, components, "
            "improvements, runtime hints, tasks, or docs sections."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "Handoff memory UUID returned by pickup_handoff"},
                "ref_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset like ['laws','components','tasks']",
                    "default": [],
                },
                "limit_per_type": {"type": "integer", "default": 3, "description": "Max resolved items per ref type"},
            },
        },
    },
    {
        "name": "refresh_handoff_context",
        "description": (
            "Refresh the compact project context attached to a handoff packet from current project knowledge. "
            "Use this when resuming an older handoff so its summary and refs reflect the latest state."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "Handoff memory UUID"},
                "task_description": {"type": "string", "description": "Optional task text override used for refresh"},
                "owner_agent": {"type": "string", "description": "Optional agent that owns the write scope for this packet"},
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional bounded areas this packet may modify", "default": []},
                "max_components": {"type": "integer", "default": 3, "description": "Max components to include in refreshed refs"},
            },
        },
    },
    {
        "name": "update_handoff_status",
        "description": (
            "Update the lifecycle status of a task packet/handoff. "
            "Use this to mark a packet as active, paused, closed, or archived after pickup."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["memory_id", "status"],
            "properties": {
                "memory_id": {"type": "string", "description": "Handoff memory UUID"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "picked_up", "active", "paused", "closed", "archived"],
                },
                "acted_by": {"type": "string", "description": "Who is updating the packet lifecycle", "default": "user"},
                "owner_agent": {"type": "string", "description": "Optional agent that owns the write scope for this packet"},
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional bounded areas this packet may modify", "default": []},
                "executor_used": {"type": "string", "description": "Optional executor used for the packet lifecycle update"},
                "model_used": {"type": "string", "description": "Optional model used for the packet lifecycle update"},
                "result_summary": {"type": "string", "description": "Short summary of the bounded result being merged back"},
                "verification_summary": {"type": "string", "description": "Short verification note for the bounded result"},
                "reason": {"type": "string", "description": "Short reason for the status change", "default": ""},
            },
        },
    },
    {
        "name": "resume_handoff",
        "description": (
            "Resume a paused or picked-up task packet. "
            "Sets the packet status to active and optionally refreshes compact project context from current knowledge."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "Handoff memory UUID"},
                "refresh_context": {"type": "boolean", "default": True},
                "task_description": {"type": "string", "description": "Optional task text override used during refresh"},
                "owner_agent": {"type": "string", "description": "Optional agent that owns the write scope for this packet"},
                "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional bounded areas this packet may modify", "default": []},
                "max_components": {"type": "integer", "default": 3, "description": "Max components to include if refresh runs"},
                "acted_by": {"type": "string", "default": "user"},
                "reason": {"type": "string", "default": "resume"},
            },
        },
    },
    {
        "name": "ingest_dir",
        "description": "Recursively scan a directory, parse all supported files, and store their contents as memories.",
        "inputSchema": {
            "type": "object",
            "required": ["path", "agent_id"],
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the directory"},
                "cwd": {"type": "string", "description": "Base directory for resolving a relative path"},
                "agent_id": {"type": "string"},
                "memory_type": {"type": "string", "default": "context"},
                "category": {"type": "string", "default": "document"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "extensions": {"type": "array", "items": {"type": "string"}, "default": []},
                "recursive": {"type": "boolean", "default": True},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "search_project_knowledge",
        "description": (
            "Search the project knowledge cache for components relevant to a query. "
            "Returns component summaries (purpose, implementation, key files) without reading source code. "
            "Use this instead of grep/glob to understand what a component does. "
            "RepRap principle: the project documents itself so you don't start from scratch each session."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "query"],
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier, e.g. 'supermemory'"},
                "query": {"type": "string", "description": "Natural language query, e.g. 'layout fixer', 'skill crystallization'"},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
        },
    },
    {
        "name": "enrich_task_with_context",
        "description": (
            "Enrich a task description with relevant project component context. "
            "Call this at the start of a task to instantly get: which components are relevant, "
            "their purpose, implementation notes, and key files to look at. "
            "Replaces the grep → read → understand loop with a single call."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "task"],
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier"},
                "task": {"type": "string", "description": "Task description to enrich with context"},
                "max_components": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
            },
        },
    },
    tool_definition("get_project_readiness"),
    tool_definition("get_project_bootstrap_checklist"),
    tool_definition("plan_remote_snapshot"),
    tool_definition("sync_remote_snapshot"),
    tool_definition("get_storage_trust_status"),
    tool_definition("review_improvement"),
    tool_definition("send_coordination_message"),
    tool_definition("pickup_coordination_messages"),
    tool_definition("list_coordination_messages"),
    tool_definition("update_coordination_message_status"),
    {
        "name": "get_task_status",
        "description": (
            "Check the status of a background job submitted via ?background=true. "
            "Returns status (queued/running/done/failed) and result when complete. "
            "Use after submitting project_ingest, project_refresh, or skills_retag in background mode."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {"type": "string", "description": "Job ID returned by background submission"},
            },
        },
    },
]

sync_tool_definitions(
    TOOLS,
    "list_learning_candidates",
    "approve_learning_candidate",
    "defer_learning_candidate",
    "reject_learning_candidate",
    "enrich_task_with_context",
    "get_project_readiness",
    "get_project_bootstrap_checklist",
    "plan_remote_snapshot",
    "sync_remote_snapshot",
    "get_storage_trust_status",
    "review_improvement",
    "send_coordination_message",
    "pickup_coordination_messages",
    "list_coordination_messages",
    "update_coordination_message_status",
    "get_artifact",
    "list_artifacts",
    "list_open_tasks",
    "normalize_mcp_intent",
    "project_workflow",
    "project_workflow_submit",
    "continue_task",
    "reopen_task",
    "list_tool_families",
    "tool_family_tools",
    "tool_explain",
    "tool_recommend",
    "tool_feedback",
    "record_task_checkpoint",
    "report_task_checkpoint",
    "resolve_artifact",
    "reopen_artifact",
    "set_canonical_status",
    "merge_canonicals",
)


# ── Async tool execution ───────────────────────────────────────────────────────

def _api_headers() -> dict:
    """Return auth headers for internal API calls."""
    from app.config import settings
    if settings.api_key:
        return {"X-Api-Key": settings.api_key}
    return {}


async def _post(api_base: str, path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=60.0, headers=_api_headers()) as c:
        r = await c.post(f"{api_base}{path}", json=payload)
        r.raise_for_status()
        return r.json()


async def _get(api_base: str, path: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as c:
        r = await c.get(f"{api_base}{path}")
        r.raise_for_status()
        return r.json()


_CHECKPOINT_HANDOFF_STAGES = {"blocked", "interrupted", "handoff", "completed"}


def _checkpoint_handoff_label(args: dict[str, Any], stage: str) -> str:
    label = str(args.get("handoff_label") or "").strip().lower()
    if not label:
        task_id = re.sub(r"[^a-z0-9_-]+", "-", str(args.get("task_id") or "").strip().lower()).strip("-_")
        label = f"checkpoint-{(task_id or 'task')[:24]}-{stage}"
    label = re.sub(r"[^a-z0-9_-]+", "-", label).strip("-_")
    if not label or not re.match(r"^[a-z0-9]", label):
        label = f"checkpoint-{label or stage}"
    return label[:64]


def _checkpoint_handoff_payload(args: dict[str, Any], *, stage: str, status: str) -> dict[str, Any]:
    def _string_list(name: str) -> list[str]:
        value = args.get(name) or []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value if str(item).strip()]

    project = str(args["project"]).strip()
    task_id = str(args["task_id"]).strip()
    summary = str(args["summary"]).strip()
    next_step = str(args.get("next_step") or "").strip()
    blockers = _string_list("blockers")
    decisions = _string_list("decisions")
    verification = _string_list("verification")
    remaining_risk = _string_list("remaining_risk")
    changed_files = _string_list("changed_files")
    acted_by = str(args.get("acted_by") or "mcp-agent").strip() or "mcp-agent"
    to_agent = str(args.get("to_agent") or acted_by).strip() or acted_by
    key_facts = [*decisions, *verification, *remaining_risk][:10]
    partial_parts = []
    if blockers:
        partial_parts.append("Blockers: " + "; ".join(blockers))
    if next_step:
        partial_parts.append("Next step: " + next_step)
    if remaining_risk:
        partial_parts.append("Remaining risk: " + "; ".join(remaining_risk))
    return {
        "from_agent": acted_by,
        "to_agent": to_agent,
        "project_id": project,
        "phase": stage,
        "priority": "high" if blockers or stage in {"blocked", "interrupted"} else "medium",
        "owner_agent": to_agent,
        "write_scope": changed_files or args.get("write_scope", []),
        "why_now": str(args.get("reason") or f"Resume-relevant checkpoint at stage={stage}.").strip(),
        "definition_of_done": "Resume from this checkpoint, preserve recorded task state, and update task progress before stopping.",
        "expected_output_shape": "Short result summary, verification summary, remaining risks, and next checkpoint.",
        "phase_objective": next_step or summary,
        "execution_mode": "balanced",
        "task_description": f"Checkpoint for task {task_id}: {summary}",
        "partial_result": "\n".join(partial_parts) or None,
        "key_facts": key_facts,
        "task_id": task_id,
        "handoff_label": _checkpoint_handoff_label(args, stage),
        "reason": "checkpoint",
        "agent_id": "handoff",
    }


def _parse_task_checkpoint_change(change: dict[str, Any] | None) -> dict[str, Any] | None:
    if not change:
        return None
    content = str(change.get("content") or "")
    parsed: dict[str, Any] = {
        "id": change.get("id"),
        "timestamp": change.get("timestamp"),
        "tags": change.get("tags") or [],
        "raw_content": content,
    }
    list_fields = {
        "Blockers": "blockers",
        "Decisions": "decisions",
        "Changed files": "changed_files",
        "Verification": "verification",
        "Remaining risk": "remaining_risk",
    }
    scalar_fields = {
        "Checkpoint stage": "stage",
        "Checkpoint status": "status",
        "Summary": "summary",
        "Next step": "next_step",
        "Reason": "reason",
    }
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in scalar_fields:
            parsed[scalar_fields[key]] = value
        elif key in list_fields:
            parsed[list_fields[key]] = [item.strip() for item in value.split(";") if item.strip()]
    return parsed


def _compact_task_history(changes: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for change in (changes or [])[-limit:]:
        compact.append(
            {
                "id": change.get("id"),
                "change_type": change.get("change_type"),
                "timestamp": change.get("timestamp"),
                "content": str(change.get("content") or "").strip(),
                "why": str(change.get("why") or "").strip(),
                "agent_id": change.get("agent_id"),
                "source": change.get("source"),
                "tags": change.get("tags") or [],
            }
        )
    return compact


async def _fetch_linked_improvement_bundle(api_base: str, project: str, linked_improvement_id: str) -> dict[str, Any] | None:
    linked_id = str(linked_improvement_id or "").strip()
    if not linked_id:
        return None
    artifact_key = f"improvement:{project}:{linked_id}"
    try:
        artifact = await _get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
    except Exception:
        return {
            "artifact_key": artifact_key,
            "id": linked_id,
            "available": False,
        }
    return {
        "artifact_key": artifact_key,
        "id": linked_id,
        "available": True,
        "title": artifact.get("title"),
        "status": artifact.get("status"),
        "stage": artifact.get("stage"),
        "verdict": artifact.get("verdict"),
        "linked_artifact_key": artifact.get("linked_artifact_key"),
        "linked_status": artifact.get("linked_status"),
    }


def _build_replay_bundle(
    *,
    project: str,
    task_id: str,
    statement: dict[str, Any],
    changes: list[dict[str, Any]],
    handoffs: list[dict[str, Any]],
    linked_improvement: dict[str, Any] | None,
) -> dict[str, Any]:
    quality = statement.get("quality") or {}
    return {
        "task_history": _compact_task_history(changes),
        "linked_improvement": linked_improvement,
        "handoff_refs": [
            {
                "memory_id": item.get("memory_id"),
                "handoff_label": item.get("handoff_label"),
                "status": item.get("status"),
                "phase": item.get("phase"),
                "task_id": item.get("task_id"),
            }
            for item in handoffs[:5]
        ],
        "project_context_refs": {
            "project_id": project,
            "task_id": task_id,
            "grounded_by": quality.get("grounded_by") or [],
            "readiness_tool": "get_project_readiness",
            "enrichment_tool": "enrich_task_with_context",
        },
    }


def _estimate_response_tokens(payload: dict[str, Any], budget_args: dict[str, Any] | None = None) -> dict[str, Any]:
    response_chars = len(json.dumps(payload, ensure_ascii=False))
    return build_token_budget(response_chars=response_chars, **(budget_args or {}))


def _layer_summary(full_payload: dict[str, Any]) -> dict[str, Any]:
    bundle = full_payload.get("replay_bundle") or {}
    return {
        "task_history": {
            "available": bool(bundle.get("task_history")),
            "count": len(bundle.get("task_history") or []),
            "request": {"detail": "full"},
        },
        "linked_improvement": {
            "available": bool(bundle.get("linked_improvement")),
            "request": {"detail": "full"},
        },
        "handoff_refs": {
            "available": bool(bundle.get("handoff_refs")),
            "count": len(bundle.get("handoff_refs") or []),
            "request": {"detail": "full"},
        },
        "project_context_refs": {
            "available": bool(bundle.get("project_context_refs")),
            "request": {"detail": "full"},
        },
        "resume_handoffs": {
            "available": bool(full_payload.get("resume_handoffs")),
            "count": len(full_payload.get("resume_handoffs") or []),
            "request": {"detail": "full"},
        },
        "next_actions": {
            "available": bool(full_payload.get("next_actions")),
            "count": len(full_payload.get("next_actions") or []),
            "request": {"detail": "full"},
        },
    }


def _project_continue_task_response(full_payload: dict[str, Any], *, detail: str, include_replay_bundle: bool, budget_args: dict[str, Any]) -> dict[str, Any]:
    if detail == "full" or include_replay_bundle:
        payload = dict(full_payload)
        payload["detail"] = "full"
        payload["available_layers"] = _layer_summary(full_payload)
        payload["token_budget"] = _estimate_response_tokens(payload, budget_args)
        payload["token_overhead"] = payload["token_budget"]
        return payload

    latest_checkpoint = dict(full_payload.get("latest_checkpoint") or {})
    latest_checkpoint.pop("raw_content", None)
    compact = {
        "project": full_payload.get("project"),
        "task_id": full_payload.get("task_id"),
        "status": full_payload.get("status"),
        "detail": "compact",
        "task": full_payload.get("task"),
        "latest_checkpoint": latest_checkpoint or None,
        "next_safe_action": full_payload.get("next_safe_action"),
        "replay_completeness": full_payload.get("replay_completeness"),
        "execution_readiness": full_payload.get("execution_readiness"),
        "replay_drill": full_payload.get("replay_drill"),
        "recommended_first_tool": full_payload.get("recommended_first_tool"),
        "task_statement_quality": full_payload.get("task_statement_quality"),
        "pending_capture_review_count": full_payload.get("pending_capture_review_count", 0),
        "promoted_capture_review_count": full_payload.get("promoted_capture_review_count", 0),
        "available_layers": _layer_summary(full_payload),
    }
    compact["token_budget"] = _estimate_response_tokens(compact, budget_args)
    compact["token_overhead"] = compact["token_budget"]
    return compact


async def _build_continue_task_payload(api_base: str, args: dict[str, Any]) -> dict[str, Any]:
    project = str(args.get("project") or "supermemory").strip() or "supermemory"
    task_id = str(args.get("task_id") or "").strip()
    limit = int(args.get("limit", 10))
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    include_replay_bundle = bool(args.get("include_replay_bundle", False))
    budget_args = {
        "model_context_window": args.get("model_context_window"),
        "resume_budget_ratio": args.get("resume_budget_ratio"),
        "resume_budget_profile": str(args.get("resume_budget_profile") or "normal"),
    }
    selected_task = None
    if not task_id:
        listed = await _get(api_base, f"/artifacts?project={quote(project, safe='')}&status=open&type=task&limit={limit}")
        items = listed.get("items") or []
        selected_task = items[0] if items else None
        task_id = str((selected_task or {}).get("task_id") or "").strip()
    if not task_id:
        payload = {
            "project": project,
            "task_id": "",
            "status": "no_open_task",
            "next_safe_action": "Create or reopen a project task before continuing.",
        }
        payload["token_budget"] = _estimate_response_tokens(payload, budget_args)
        payload["token_overhead"] = payload["token_budget"]
        return payload

    statement = await _get(api_base, f"/project/tasks/{quote(task_id, safe='')}/statement?project={quote(project, safe='')}")
    changes = await _get(api_base, f"/project/tasks/{quote(task_id, safe='')}/changes?project={quote(project, safe='')}&limit=100")
    checkpoint_changes = [
        change for change in (changes or [])
        if "task_checkpoint" in {str(tag).strip() for tag in (change.get("tags") or [])}
        or "[task_checkpoint]" in str(change.get("content") or "")
    ]
    latest_checkpoint = _parse_task_checkpoint_change(checkpoint_changes[-1] if checkpoint_changes else None)
    next_actions = statement.get("next_actions") or []
    next_safe_action = (
        str((latest_checkpoint or {}).get("next_step") or "").strip()
        or str((next_actions[0] if next_actions else {}).get("action") or "").strip()
        or "Inspect the current task statement and record a planning checkpoint."
    )
    handoffs: list[dict[str, Any]] = []
    if bool(args.get("include_handoffs", True)):
        try:
            handoff_result = await _post(
                api_base,
                "/models/handoff/list",
                {
                    "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
                    "statuses": ["pending", "picked_up", "active", "paused"],
                    "limit": max(limit, 10),
                    "compact": True,
                },
            )
            handoffs = [
                item for item in (handoff_result.get("handoffs") or [])
                if str(item.get("task_id") or "").strip() == task_id
            ]
        except Exception:
            handoffs = []

    quality = statement.get("quality") or {}
    capture_review = statement.get("capture_review") or {}
    task = statement.get("task") or selected_task or {}
    linked_improvement = await _fetch_linked_improvement_bundle(api_base, project, str(task.get("linked_improvement_id") or ""))
    payload = {
        "project": project,
        "task_id": task_id,
        "status": "ready",
        "task": {
            "title": task.get("title"),
            "status": task.get("status"),
            "linked_improvement_id": task.get("linked_improvement_id"),
        },
        "latest_checkpoint": latest_checkpoint,
        "next_safe_action": next_safe_action,
        "task_statement_quality": quality,
        "pending_capture_review_count": capture_review.get("pending_count", 0),
        "promoted_capture_review_count": capture_review.get("promoted_count", 0),
        "next_actions": next_actions[:5],
        "resume_handoffs": handoffs[:5],
        "recommended_first_tool": "record_task_checkpoint" if not latest_checkpoint else "continue_task",
    }
    payload["replay_bundle"] = _build_replay_bundle(
        project=project,
        task_id=task_id,
        statement=statement,
        changes=changes or [],
        handoffs=handoffs,
        linked_improvement=linked_improvement,
    )
    payload["replay_completeness"] = evaluate_replay_completeness(payload)
    payload["execution_readiness"] = evaluate_execution_readiness(payload)
    payload["replay_drill"] = build_replay_drill_decision(payload)
    if payload["replay_completeness"]["status"] == "incomplete":
        payload["recommended_first_tool"] = "record_task_checkpoint"
    elif payload["execution_readiness"]["status"] == "incomplete":
        payload["recommended_first_tool"] = "record_task_checkpoint"
    elif payload["replay_drill"]["status"] == "ready":
        payload["recommended_first_tool"] = payload["replay_drill"]["first_tool"]
    return _project_continue_task_response(payload, detail=detail, include_replay_bundle=include_replay_bundle, budget_args=budget_args)


async def _delete(api_base: str, path: str, payload: dict | None = None) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as c:
        if payload is not None:
            r = await c.request("DELETE", f"{api_base}{path}", json=payload)
        else:
            r = await c.delete(f"{api_base}{path}")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()


async def _patch(api_base: str, path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as c:
        r = await c.patch(f"{api_base}{path}", json=payload or {})
        r.raise_for_status()
        return r.json()


def _operation_path(operation: dict[str, Any]) -> str:
    operation_type = operation["type"]
    path_templates = {
        "record_task_change": lambda op: f"/project/tasks/{quote(op['task_id'], safe='')}/changes",
        "resolve_artifact": lambda op: f"/artifacts/{quote(op['artifact_key'], safe='')}/resolve",
    }
    if operation_type not in path_templates:
        raise ValueError(f"Unsupported MCP operation type: {operation_type}")
    return path_templates[operation_type](operation)


async def _execute_mcp_operation_plan(api_base: str, plan: dict[str, Any]) -> dict[str, Any]:
    receipt: dict[str, Any] = dict(plan["receipt"])
    for operation in plan.get("operations") or []:
        result = await _post(api_base, _operation_path(operation), operation["payload"])
        receipt[operation["result_key"]] = result
        receipt.setdefault("routed_to", []).append(operation["route_label"])
    return receipt


def _format_tool_error_brief(exc: Exception, *, default: str = "request failed") -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else "error"
        detail = ""
        response = exc.response
        if response is not None:
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = str(
                        payload.get("detail")
                        or payload.get("error")
                        or payload.get("message")
                        or ""
                    )
            except Exception:
                detail = ""
            if not detail:
                detail = str(response.text or "")
        if detail:
            compact = re.sub(r"\s+", " ", detail).strip()[:180]
            return f"HTTP {status}: {compact}"
        return f"HTTP {status}"

    message = re.sub(r"\s+", " ", str(exc or "")).strip()
    if not message:
        return default
    return message[:180]


async def _execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None) -> str:
    await _session_observe(session_id, name, args)
    try:
        observe_tool_use(name)
    except Exception:
        pass

    # Server-side observer: works for any MCP client, no client hooks needed.
    # Runs asynchronously and never blocks tool execution.
    asyncio.create_task(_mcp_live_observe(name, args, api_base))

    if name == "memory_store":
        data = await _post(api_base, "/memories", args)
        return f"Stored memory {data['id']}\n{json.dumps(data, indent=2, ensure_ascii=False)}"

    elif name == "memory_search":
        results = await _post(api_base, "/memories/search", args)
        if not results:
            return "No memories found."
        lines = []
        for r in results:
            m = r["memory"]
            lines.append(f"[{r['score']:.3f}] ({m['memory_type']}) {m['content'][:200]}\n  id={m['id']}")
        return "\n\n".join(lines)

    elif name == "memory_tree_slice":
        data = await _post(api_base, "/knowledge-tree/slice", args)
        res = [f"Target Category: {data.get('target_category', 'general')}\n"]
        for r in data.get("results", []):
            m = r.get("memory", {})
            res.append(
                f"[{r.get('score', 0):.3f} | boost:+{r.get('tree_boost', 0):.3f}] ({m.get('category', 'general')}) {m.get('content', '')[:200]}\n  id={m.get('id')} path={m.get('topic_path')}"
            )
        return "\n\n".join(res)

    elif name == "memory_context":
        data = await _post(api_base, "/memories/context", args)
        sid = data.get("session_id") or "—"
        ctx = (data.get("context") or "")
        snippet = ctx[:800]
        more = "…" if len(ctx) > len(snippet) else ""
        return (
            f"session_id={sid} used={data.get('used_count',0)} sources={data.get('source_count',0)} "
            f"scope_expanded={bool(data.get('scope_expanded'))}\n\n"
            f"{snippet}{more}"
        )

    elif name == "record_memory_outcome":
        data = await _post(api_base, "/outcomes", args)
        return (
            f"Recorded outcome: success={data.get('success')} session_id={data.get('session_id') or args.get('session_id')}\n"
            f"updated={data.get('updated',0)} skipped={data.get('skipped',0)}"
        )

    elif name == "memory_recent":
        params = f"?minutes={args.get('minutes', 10)}&limit={args.get('limit', 20)}"
        if args.get("agent_id"):
            params += f"&agent_id={args['agent_id']}"
        results = await _get(api_base, f"/memories/recent{params}")
        if not results:
            return "No recent memories found."
        lines = []
        for m in results:
            lines.append(f"[{m['timestamp'][:19]}] ({m['agent_id']}) {m['content'][:200]}\n  id={m['id']}")
        return "\n\n".join(lines)

    elif name == "memory_get":
        data = await _get(api_base, f"/memories/{args['memory_id']}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "memory_delete":
        await _delete(api_base, f"/memories/{args['memory_id']}")
        return f"Deleted memory {args['memory_id']}"

    elif name == "memory_batch_store":
        # MCP clients sometimes serialize array args as JSON strings — normalise
        import json as _json
        memories = args.get("memories", [])
        if isinstance(memories, str):
            memories = _json.loads(memories)
        data = await _post(api_base, "/memories/batch", {"memories": memories})
        return f"Created {len(data['created_ids'])} memories. Failed: {data['failed_count']}"

    elif name == "memory_cleanup":
        data = await _delete(api_base, "/memories/cleanup", args)
        return f"Deleted {data['deleted_count']} memories."

    elif name == "system_info":
        data = await _get(api_base, "/system/info")
        infra = data.get("infrastructure", {})
        counters = data.get("counters", {})
        components = data.get("components", [])
        models = infra.get("ollama", {}).get("models", [])

        lines = [
            f"supermemory — status: {data.get('status','?')} | uptime: {data.get('uptime_seconds',0)//60}m",
            f"Qdrant: {'✓' if infra.get('qdrant',{}).get('reachable') else '✗'}  "
            f"Ollama: {'✓' if infra.get('ollama',{}).get('reachable') else '✗'}  "
            f"embedding: {infra.get('embedding_model','?')} ({infra.get('embedding_dimensions','?')}d)",
            f"Models: {', '.join(models) or 'none'}",
            f"",
            f"Counters: memories={counters.get('memories',0)}  "
            f"skills={counters.get('skills',0)}  "
            f"layout_terms={counters.get('layout_terms',0)}",
            f"",
            f"Components ({len(components)}):",
        ]
        for c in components:
            tag = "[core]" if c.get("status") == "core" else "[opt] "
            lines.append(f"  {tag} {c['id']:20s} — {c['description'][:80]}")
            endpoints = c.get("endpoints") or []
            if endpoints:
                lines.append(f"    endpoints: {', '.join(endpoints)}")

        return "\n".join(lines)

    elif name == "memory_stats":
        data = await _get(api_base, "/memories/stats")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "registry_best":
        params = f"task_type={args['task_type']}&top={args.get('top', 3)}"
        if args.get("exclude"):
            params += f"&exclude={args['exclude']}"
        data = await _get(api_base, f"/registry/best?{params}")
        lines = [f"Best components for '{data['task_type']}':"]
        for i, r in enumerate(data["ranked"], 1):
            bar = "█" * int(r["score"] * 10) + "░" * (10 - int(r["score"] * 10))
            lines.append(f"  {i}. {r['component']:20s} {bar} {r['score']:.3f}")
        return "\n".join(lines)

    elif name == "registry_update":
        data = await _post(api_base, "/registry/update", args)
        status = "✓" if args.get("success") else "✗"
        return f"{status} Updated {data['component']} / {data['task_type']} → score: {data['new_score']}"

    elif name == "registry_components":
        data = await _get(api_base, "/registry/components")
        lines = []
        for comp, caps in data.items():
            lines.append(f"\n{comp}:")
            for task, info in sorted(caps.items(), key=lambda x: -x[1]["score"]):
                bar = "█" * int(info["score"] * 10)
                lines.append(f"  {task:25s} {bar} {info['score']:.2f}  ({info['success']}✓/{info['fail']}✗)")
        return "\n".join(lines)

    elif name == "report_issue":
        data = await _post(api_base, "/improvements", args)
        return f"Improvement reported: {data['id']}\nTitle: {data['title']}\nStatus: {data['status']}"

    elif name == "review_improvement":
        improvement_id = args["improvement_id"]
        payload = {
            key: args[key]
            for key in ("stage", "verdict", "reviewed_by", "review_source", "reason")
            if args.get(key) is not None
        }
        data = await _patch(api_base, f"/improvements/{improvement_id}/review", payload)
        lines = [
            f"Improvement reviewed: {data['id']}",
            f"Title: {data['title']}",
            f"Stage: {data.get('stage') or 'proposal'}",
            f"Verdict: {data.get('verdict') or 'unset'}",
            f"Status: {data['status']}",
        ]
        return "\n".join(lines)

    elif name == "list_project_laws":
        params = [
            f"status={args.get('status', 'active')}",
            f"limit={int(args.get('limit', 20))}",
            f"include_promoted={str(bool(args.get('include_promoted', True))).lower()}",
        ]
        if args.get("project"):
            params.append(f"project={args['project']}")
        if args.get("scope"):
            params.append(f"scope={args['scope']}")
        data = await _get(api_base, f"/laws?{'&'.join(params)}")
        items = data.get("items", [])
        if not items:
            return "No matching project laws."
        lines = []
        for i, item in enumerate(items, 1):
            locality = "project-local" if item.get("is_project_local") else item.get("scope", "?")
            lines.append(
                f"{i}. [{item.get('status','?')}] {item.get('title','')}\n"
                f"   scope={item.get('scope','?')} locality={locality} project={item.get('project') or '-'}\n"
                f"   id={item.get('id')}"
            )
        project = args.get("project", "all")
        status = args.get("status", "active")
        return f"Project laws ({project}, {status}):\n\n" + "\n\n".join(lines)

    elif name == "get_project_law":
        data = await _get(api_base, f"/laws/{args['law_id']}")
        lines = [
            f"title={data.get('title','')}",
            f"status={data.get('status','?')} scope={data.get('scope','?')} project={data.get('project') or '-'} version={data.get('version','1.0')}",
            f"statement={data.get('statement','')}",
        ]
        if data.get("rationale"):
            lines.append(f"rationale={data['rationale']}")
        evidence = data.get("evidence") or []
        if evidence:
            lines.append("evidence:")
            lines.extend(f"- {item}" for item in evidence[:5])
        candidate = data.get("candidate_revision")
        if candidate:
            lines.append(f"candidate_status={candidate.get('status', 'proposed')}")
            lines.append(f"candidate_statement={candidate.get('statement', '')}")
        if data.get("confirmed_by"):
            lines.append(f"confirmed_by={data.get('confirmed_by')}")
        lines.append(f"id={data.get('id')}")
        return "\n".join(lines)

    elif name == "list_learning_candidates":
        query = build_list_learning_candidates_query(args)
        data = await _get(api_base, f"/learning/artifacts?{query}")
        return format_list_learning_candidates_response(data)

    elif name == "approve_learning_candidate":
        data = await _post(
            api_base,
            f"/learning/candidates/{args['artifact_id']}/approve",
            build_approve_learning_candidate_payload(args),
        )
        return format_learning_candidate_transition(data, action="approved")

    elif name == "defer_learning_candidate":
        data = await _post(
            api_base,
            f"/learning/candidates/{args['artifact_id']}/defer",
            build_defer_learning_candidate_payload(args),
        )
        return format_learning_candidate_transition(data, action="deferred")

    elif name == "reject_learning_candidate":
        data = await _post(
            api_base,
            f"/learning/candidates/{args['artifact_id']}/reject",
            build_reject_learning_candidate_payload(args),
        )
        return format_learning_candidate_transition(data, action="rejected")

    elif name == "improvements_report":
        project = args.get("project", "supermemory")
        data = await _get(api_base, f"/improvements/report?project={project}")
        s = data["stats"]
        lines = [
            f"## Project Status: {s['project']}",
            f"Total: {s['total']} | Resolved: {s['resolved']} ({s['resolved_pct']}%) | Open: {s['open']}",
            f"Top tags: {', '.join(t['tag'] for t in s['top_tags'])}",
        ]
        if s["top_open"]:
            lines.append("\n**Open (by importance):**")
            for item in s["top_open"]:
                lines.append(f"- [{item['importance']:.2f}] {item['title']}  id={item['id']}")
        if s["top_resolved"]:
            lines.append("\n**Top resolved:**")
            for item in s["top_resolved"]:
                lines.append(f"- [{item['importance']:.2f}] {item['title']}")
        if data.get("narrative"):
            lines.append("\n---\n")
            lines.append(data["narrative"])
        return "\n".join(lines)

    elif name == "knowledge_hierarchy":
        params = [
            f"include_suppressed={str(bool(args.get('include_suppressed', False))).lower()}",
            f"limit_per_scope={int(args.get('limit_per_scope', 25))}",
            f"reconcile={str(bool(args.get('reconcile', False))).lower()}",
        ]
        if args.get("topic_prefix"):
            params.append(f"topic_prefix={args['topic_prefix']}")
        data = await _get(api_base, f"/knowledge-hierarchy?{'&'.join(params)}")
        totals = data.get("totals", {})
        lifecycle = data.get("lifecycle", {})
        lines = [
            f"Knowledge hierarchy topic_prefix={data.get('topic_prefix') or 'all'}",
            f"domain={totals.get('domain',0)} principle={totals.get('principle',0)} meta={totals.get('meta',0)}",
            f"lifecycle: active={lifecycle.get('active',0)} suppressed={lifecycle.get('suppressed',0)} updated={lifecycle.get('updated',0)}",
        ]
        for scope in ("domain", "principle", "meta"):
            items = data.get("by_scope", {}).get(scope, [])
            if not items:
                continue
            lines.append(f"\n[{scope}]")
            for item in items[:10]:
                status = item.get("canonical_status") or ("suppressed" if item.get("suppressed") else "active")
                lines.append(
                    f"- {item.get('topic_path','?')} | supports={item.get('support_count',0)} | "
                    f"confidence={item.get('confidence',0):.2f} | status={status} | id={item.get('id')}"
                )
        return "\n".join(lines)

    elif name == "canonicals_by_scope":
        params = [
            f"scope={args['scope']}",
            f"include_suppressed={str(bool(args.get('include_suppressed', False))).lower()}",
            f"limit={int(args.get('limit', 50))}",
        ]
        if args.get("topic_prefix"):
            params.append(f"topic_prefix={args['topic_prefix']}")
        data = await _get(api_base, f"/canonicals/by-scope?{'&'.join(params)}")
        items = data.get("items", [])
        if not items:
            return f"No canonicals for scope '{args['scope']}'."
        lines = [f"Canonicals ({args['scope']}):"]
        for item in items:
            status = item.get("canonical_status") or ("suppressed" if item.get("suppressed") else "active")
            lines.append(
                f"- {item.get('topic_path','?')} | supports={item.get('support_count',0)} | "
                f"confidence={item.get('confidence',0):.2f} | status={status}\n  id={item.get('id')}"
            )
        return "\n".join(lines)

    elif name == "set_canonical_status":
        data = await _patch(
            api_base,
            f"/canonicals/{args['canonical_id']}/status",
            build_set_canonical_status_payload(args),
        )
        return format_set_canonical_status_response(data)

    elif name == "merge_canonicals":
        data = await _post(
            api_base,
            f"/canonicals/{args['source_id']}/merge",
            build_merge_canonicals_payload(args),
        )
        return (
            f"Merged canonical {data['source_id']} → {data['target_id']}\n"
            f"topic_path={data['topic_path']} supports={data['merged_support_count']}"
        )

    elif name == "crystallize_solution":
        data = await _post(api_base, "/crystallizer/crystallize", args)
        if data["crystallized"]:
            return (
                f"✨ Skill crystallized: '{data['skill_name']}'\n"
                f"Score: {data['reusability_score']:.2f} | id: {data['skill_id']}\n"
                f"Reason: {data['reason']}\n"
                f"Next time this task is routed to skill tier (instant/free)."
            )
        else:
            return (
                f"⏭ Not crystallized (score {data['reusability_score']:.2f} < threshold)\n"
                f"Reason: {data['reason']}"
            )

    elif name == "draft_skill":
        data = await _post(api_base, "/crystallizer/draft", args)
        if not data["draft_ready"]:
            return (
                f"⏭ Draft not generated (score {data['reusability_score']:.2f} < threshold)\n"
                f"Reason: {data['reason']}"
            )
        publish_hint = (
            "✅ High score — recommended to publish as-is via skill_publish."
            if data.get("auto_publish_recommended")
            else "📝 Review the draft and edit if needed, then call skill_publish."
        )
        return (
            f"📋 Skill draft ready: '{data['skill_name']}'\n"
            f"Score: {data['reusability_score']:.2f} | {publish_hint}\n"
            f"Reason: {data['reason']}\n\n"
            f"--- SKILL.md draft ---\n{data['skill_content']}\n--- end draft ---\n\n"
            f"Call skill_publish(name='{data['skill_name']}', content=<above or edited>, "
            f"platform='{data.get('platform', 'claude')}') to publish."
        )

    elif name == "model_available":
        params = ""
        if args.get("task_type"):
            params = f"?task_type={args['task_type']}"
        models = await _get(api_base, f"/models/available{params}")
        if not models:
            return "No available cloud models. All models may be at quota or in cooldown."
        lines = ["Available cloud models:"]
        for m in models:
            bar = "█" * int(m["remaining_pct"] / 10) + "░" * (10 - int(m["remaining_pct"] / 10))
            lines.append(f"  {m['priority']}. {m['model_id']:15s} [{m['provider']}] {bar} {m['remaining_pct']:.0f}% remaining ({m['remaining']:,} {m['limit_unit']})")
        return "\n".join(lines)

    elif name == "report_limit_hit":
        data = await _post(api_base, "/models/report_limit", args)
        cooldown = data.get("cooldown_until")
        if cooldown:
            import time as _time
            secs = max(0, int(cooldown - _time.time()))
            return f"⛔ {args['model_id']} marked as rate-limited. Cooldown: {secs}s. Use model_available to find alternatives."
        return f"⛔ {args['model_id']} marked as rate-limited. Use model_available to find alternatives."

    elif name == "handoff_task":
        phase = args.get("phase")
        project_id = args.get("project_id")
        phase_objective = None
        core_instinct_ids: list[str] = []
        supporting_instinct_ids: list[str] = []
        project_context_summary = None
        project_context_refs: dict[str, list[str]] = {}
        project_context_snapshot = None
        if phase:
            playbook = build_operational_instinct_playbook(
                family="task_lifecycle",
                project_id=project_id,
            )
            phase_entry = next((item for item in playbook["phases"] if item["phase"] == phase), None)
            if phase_entry:
                phase_objective = phase_entry.get("objective") or None
                core_instinct_ids = list(phase_entry.get("core_instinct_ids") or [])
                supporting_instinct_ids = list(phase_entry.get("supporting_instinct_ids") or [])
        if project_id and args.get("include_project_context", True):
            try:
                enrich_data = await _post(
                    api_base,
                    "/project/enrich-task",
                    {
                        "project_id": project_id,
                        "task": args["task_description"],
                        "max_components": int(args.get("context_max_components", 3)),
                        "context_profile": "handoff_compact",
                    },
                )
                project_context_summary = _build_handoff_context_summary(enrich_data) or None
                project_context_refs = _build_handoff_context_refs(enrich_data)
            except Exception:
                project_context_summary = None
                project_context_refs = {}
        payload = {
            "from_agent": args["from_agent"],
            "to_agent": args["to_agent"],
            "project_id": project_id,
            "phase": phase,
            "priority": args.get("priority"),
            "owner_agent": args.get("owner_agent"),
            "write_scope": args.get("write_scope", []),
            "why_now": args.get("why_now"),
            "definition_of_done": args.get("definition_of_done"),
            "expected_output_shape": args.get("expected_output_shape"),
            "phase_objective": phase_objective,
            "execution_mode": args.get("execution_mode", "balanced"),
            "background_job_type": args.get("background_job_type"),
            "background_payload": args.get("background_payload") or {},
            "core_instinct_ids": core_instinct_ids,
            "supporting_instinct_ids": supporting_instinct_ids,
            "project_context_summary": project_context_summary,
            "project_context_refs": project_context_refs,
            "project_context_snapshot": project_context_snapshot,
            "task_description": args["task_description"],
            "partial_result": args.get("partial_result"),
            "key_facts": args.get("key_facts", []),
            "task_id": args.get("task_id"),
            "handoff_label": args.get("handoff_label"),
            "reason": args.get("reason", "manual"),
            "from_model_id": args.get("from_model_id"),
            "agent_id": "handoff",
        }
        data = await _post(api_base, "/models/handoff", payload)
        next_models = ", ".join(m["model_id"] for m in data.get("next_available", []))
        label_line = f"handoff_label: {data['handoff_label']}\n" if data.get("handoff_label") else ""
        phase_line = f"phase: {data['phase']}\n" if data.get("phase") else ""
        priority_line = f"priority: {data['priority']}\n" if data.get("priority") else ""
        owner_agent = data.get("owner_agent") or args.get("owner_agent")
        owner_agent_line = f"owner_agent: {owner_agent}\n" if owner_agent else ""
        write_scope = data.get("write_scope") or args.get("write_scope") or []
        write_scope_line = f"write_scope: {_format_handoff_scope(write_scope)}\n" if write_scope else ""
        core_line = f"core_instinct_ids: {', '.join(data.get('core_instinct_ids') or [])}\n" if data.get("core_instinct_ids") else ""
        objective_line = f"phase_objective: {data['phase_objective']}\n" if data.get("phase_objective") else ""
        execution_mode = data.get("execution_mode") or args.get("execution_mode")
        execution_mode_line = f"execution_mode: {execution_mode}\n" if execution_mode else ""
        background_job_type = data.get("background_job_type") or args.get("background_job_type")
        background_job_type_line = f"background_job_type: {background_job_type}\n" if background_job_type else ""
        background_payload = data.get("background_payload") or args.get("background_payload") or {}
        background_payload_line = f"background_payload: {_format_handoff_background_payload(background_payload)}\n" if background_payload else ""
        context_summary_line = f"project_context_summary: {data['project_context_summary']}\n" if data.get("project_context_summary") else ""
        context_refs = data.get("project_context_refs") or {}
        context_refs_line = ""
        if context_refs:
            context_refs_line = "project_context_refs: " + ", ".join(
                f"{key}={len(values)}" for key, values in context_refs.items() if values
            ) + "\n"
        return (
            f"Task packaged for handoff\n"
            f"task_id: {data['task_id']}\n"
            f"{label_line}"
            f"{phase_line}"
            f"{priority_line}"
            f"{owner_agent_line}"
            f"{write_scope_line}"
            f"{objective_line}"
            f"{execution_mode_line}"
            f"{background_job_type_line}"
            f"{background_payload_line}"
            f"{core_line}"
            f"{context_summary_line}"
            f"{context_refs_line}"
            f"memory_id: {data['memory_id']}\n"
            f"To: {data['to_agent']}\n"
            f"Next available models: {next_models or 'none'}\n"
            f"Instruction: {data['pickup_instruction']}"
        )

    elif name == "pickup_handoff":
        data = await _post(api_base, "/models/handoff/pickup", args)
        if data["found"] == 0:
            if args.get("handoff_label"):
                return f"No pending handoffs for agent '{args['agent_id']}' with label '{args['handoff_label']}'."
            return (
                f"No pending handoffs for agent '{args['agent_id']}'. "
                f"Use list_pending_handoff_labels(agent_id='{args['agent_id']}') to inspect the queue."
            )
        lines = [f"Found {data['found']} pending handoff(s) for '{args['agent_id']}':"]
        if not args.get("handoff_label") and data["found"] > 1:
            lines.append(
                f"Tip: use list_pending_handoff_labels(agent_id='{args['agent_id']}') and then pickup_handoff(..., handoff_label='<label>') to target one task."
            )
        for i, h in enumerate(data["handoffs"], 1):
            lines.append(f"\n--- Handoff {i} ---")
            lines.append(f"task_id: {h['task_id']}")
            if h.get("handoff_label"):
                lines.append(f"handoff_label: {h['handoff_label']}")
            lines.append(f"from: {h['from_agent']}")
            lines.append(f"memory_id: {h['memory_id']}")
            if h.get("status"):
                lines.append(f"status: {h['status']}")
            if h.get("project_id"):
                lines.append(f"project_id: {h['project_id']}")
            owner_agent = _extract_handoff_field(h, "owner_agent")
            if owner_agent:
                lines.append(f"owner_agent: {owner_agent}")
            write_scope = _extract_handoff_field(h, "write_scope")
            if write_scope:
                lines.append(f"write_scope: {_format_handoff_scope(write_scope)}")
            if h.get("phase"):
                lines.append(f"phase: {h['phase']}")
            if h.get("priority"):
                lines.append(f"priority: {h['priority']}")
            if h.get("execution_mode"):
                lines.append(f"execution_mode: {h['execution_mode']}")
            if h.get("background_job_type"):
                lines.append(f"background_job_type: {h['background_job_type']}")
            if h.get("background_payload"):
                lines.append(f"background_payload: {_format_handoff_background_payload(h['background_payload'])}")
            if h.get("background_job_status"):
                lines.append(f"background_job_status: {h['background_job_status']}")
            if h.get("dispatched_job_id"):
                lines.append(f"dispatched_job_id: {h['dispatched_job_id']}")
            if h.get("definition_of_done"):
                lines.append(f"definition_of_done: {h['definition_of_done']}")
            if h.get("expected_output_shape"):
                lines.append(f"expected_output_shape: {h['expected_output_shape']}")
            if h.get("phase_objective"):
                lines.append(f"phase_objective: {h['phase_objective']}")
            if h.get("core_instinct_ids"):
                lines.append(f"core_instinct_ids: {', '.join(h['core_instinct_ids'])}")
            if h.get("supporting_instinct_ids"):
                lines.append(f"supporting_instinct_ids: {', '.join(h['supporting_instinct_ids'])}")
            if h.get("project_context_summary"):
                lines.append(f"project_context_summary: {h['project_context_summary']}")
            if h.get("project_context_refs"):
                lines.append("project_context_refs: " + _summarize_handoff_ref_counts(h.get("project_context_refs") or {}))
                lines.append(f"Use expand_handoff_refs(memory_id='{h['memory_id']}') to inspect referenced context.")
            if h.get("project_context_snapshot"):
                lines.append("project_context_snapshot:")
                lines.append(h["project_context_snapshot"][:1200])
            content_preview = _sanitize_handoff_content_preview(h.get("content") or "")
            if content_preview.strip():
                lines.append(content_preview)
        return "\n".join(lines)

    elif name == "list_pending_handoff_labels":
        qs = f"/models/handoff/pending_labels?agent_id={quote(str(args['agent_id']))}&limit={int(args.get('limit', 20))}"
        data = await _get(api_base, qs)
        if data["found"] == 0:
            return f"No pending handoff labels for agent '{args['agent_id']}'."
        lines = [f"Pending handoff labels for '{args['agent_id']}':"]
        for item in data["labels"]:
            from_agents = ", ".join(item.get("from_agents") or [])
            latest_task = item.get("latest_task_id") or "unknown"
            lines.append(
                f"- {item['handoff_label']} (count={item['count']}, latest_task_id={latest_task}, from={from_agents or 'unknown'})"
            )
        return "\n".join(lines)

    elif name == "list_handoffs":
        data = await _post(api_base, "/models/handoff/list", args)
        if data["found"] == 0:
            requested = ", ".join(data.get("statuses") or ["all"])
            return f"No handoffs for agent '{args['agent_id']}' matched statuses '{requested}'."
        lines = [f"Handoffs for '{args['agent_id']}' ({', '.join(data.get('statuses') or ['all'])}):"]
        for item in data["handoffs"]:
            label = item.get("handoff_label") or "-"
            phase = item.get("phase") or "-"
            priority = item.get("priority") or "-"
            owner_agent = _extract_handoff_field(item, "owner_agent")
            write_scope = _extract_handoff_field(item, "write_scope")
            result_summary = _extract_handoff_field(item, "result_summary")
            executor_used = _extract_handoff_field(item, "executor_used")
            model_used = _extract_handoff_field(item, "model_used")
            execution_mode = item.get("execution_mode")
            background_job_type = item.get("background_job_type")
            background_payload = item.get("background_payload")
            background_job_status = item.get("background_job_status")
            dispatched_job_id = item.get("dispatched_job_id")
            lines.append(
                f"- {item.get('task_id') or 'unknown'} label={label} status={item.get('status') or 'unknown'} "
                f"phase={phase} priority={priority} memory_id={item.get('memory_id')}"
                + (f" owner_agent={owner_agent}" if owner_agent else "")
                + (f" write_scope={_format_handoff_scope(write_scope)}" if write_scope else "")
                + (f" execution_mode={execution_mode}" if execution_mode else "")
                + (f" background_job_type={background_job_type}" if background_job_type else "")
                + (f" background_payload={_format_handoff_background_payload(background_payload)}" if background_payload else "")
                + (f" background_job_status={background_job_status}" if background_job_status else "")
                + (f" dispatched_job_id={dispatched_job_id}" if dispatched_job_id else "")
                + (f" executor_used={executor_used}" if executor_used else "")
                + (f" model_used={model_used}" if model_used else "")
                + (f" result_summary={result_summary}" if result_summary else "")
            )
        return "\n".join(lines)

    elif name == "handoff_workspace_summary":
        data = await _post(api_base, "/models/handoff/workspace_summary", args)
        return _format_handoff_workspace_summary(data)

    elif name == "decompose_task_packet":
        data = await _post(api_base, "/models/handoff/decompose", args)
        return _format_handoff_decomposition(data)

    elif name == "create_task_packets":
        payload = dict(args)
        if isinstance(payload.get("key_facts"), str):
            payload["key_facts"] = json.loads(payload["key_facts"])
        if isinstance(payload.get("packets"), str):
            payload["packets"] = json.loads(payload["packets"])
        for packet in payload.get("packets") or []:
            if packet.get("background_job_type") is None:
                packet.pop("background_job_type", None)
            if not packet.get("background_payload"):
                packet.pop("background_payload", None)
        payload["agent_id"] = payload.get("agent_id") or "handoff"
        data = await _post(api_base, "/models/handoff/create_packets", payload)
        return _format_created_task_packets(data)

    elif name == "route_task_packet_execution":
        payload = dict(args)
        if isinstance(payload.get("packet"), str):
            payload["packet"] = json.loads(payload["packet"])
        data = await _post(api_base, "/models/handoff/route_execution", payload)
        return _format_route_task_packet_execution(data)

    elif name == "dispatch_background_task_packet":
        data = await _post(api_base, "/models/handoff/dispatch_background", args)
        return _format_dispatch_background_task_packet(data)

    elif name == "reconcile_background_task_packet":
        data = await _post(api_base, "/models/handoff/reconcile_background", args)
        return _format_reconcile_background_task_packet(data)

    elif name == "expand_handoff_refs":
        data = await _post(api_base, "/models/handoff/expand_refs", args)
        lines = [f"Expanded handoff refs for {data['memory_id']}"]
        if data.get("project_id"):
            lines.append(f"project_id: {data['project_id']}")
        requested = data.get("requested_ref_types") or []
        if requested:
            lines.append("requested_ref_types: " + ", ".join(requested))
        resolved = data.get("resolved") or {}
        if not resolved:
            lines.append("No referenced context could be resolved.")
        for ref_type, items in resolved.items():
            lines.append(f"{ref_type} ({len(items)}):")
            for item in items:
                if ref_type == "laws":
                    lines.append(
                        f"- {item.get('id')} [{item.get('status')}] {item.get('title')}: {str(item.get('statement') or '')[:140]}"
                    )
                elif ref_type == "components":
                    lines.append(
                        f"- {item.get('component_id')} {item.get('name')}: {str(item.get('summary') or '')[:140]}"
                    )
                elif ref_type == "improvements":
                    lines.append(
                        f"- {item.get('id')} [{item.get('status')}] {item.get('title')}: {str(item.get('description') or '')[:140]}"
                    )
                elif ref_type == "runtime_hints":
                    lines.append(
                        f"- {item.get('id')} [{item.get('status')}] {item.get('action_type')}: {str(item.get('content') or '')[:140]}"
                    )
                elif ref_type == "tasks":
                    lines.append(
                        f"- {item.get('task_id')} [{item.get('status')}] {item.get('title')}: {str(item.get('description') or '')[:140]}"
                    )
                elif ref_type == "task_capture_candidates":
                    lines.append(
                        f"- {item.get('artifact_id')} [{item.get('status')}] {item.get('kind')} for {item.get('task_id') or 'unknown-task'}: {str(item.get('content') or '')[:140]}"
                    )
                elif ref_type == "docs_sections":
                    lines.append(
                        f"- {item.get('section_key')} {item.get('name')}: {str(item.get('content_preview') or '')[:140]}"
                    )
        unresolved = data.get("unresolved") or {}
        if unresolved:
            lines.append(
                "unresolved: " + ", ".join(f"{key}={len(values)}" for key, values in unresolved.items() if values)
            )
        return "\n".join(lines)

    elif name == "refresh_handoff_context":
        data = await _post(api_base, "/models/handoff/refresh_context", args)
        lines = [f"Refreshed handoff context for {data['memory_id']}"]
        if data.get("status"):
            lines.append(f"status: {data['status']}")
        if data.get("project_id"):
            lines.append(f"project_id: {data['project_id']}")
        if data.get("owner_agent"):
            lines.append(f"owner_agent: {data['owner_agent']}")
        if data.get("write_scope"):
            lines.append(f"write_scope: {_format_handoff_scope(data['write_scope'])}")
        if data.get("task_description"):
            lines.append(f"task: {data['task_description']}")
        if data.get("project_context_summary"):
            lines.append(f"project_context_summary: {data['project_context_summary']}")
        refs = data.get("project_context_refs") or {}
        if refs:
            lines.append("project_context_refs: " + _summarize_handoff_ref_counts(refs))
        coverage = data.get("coverage") or {}
        if coverage:
            lines.append(
                "coverage: " + ", ".join(f"{key}={value}" for key, value in coverage.items() if value)
            )
        if data.get("code_inspection_recommended"):
            lines.append("code_inspection_recommended: true")
        return "\n".join(lines)

    elif name == "update_handoff_status":
        data = await _post(api_base, "/models/handoff/status", args)
        lines = [f"Updated handoff status for {data['memory_id']}"]
        lines.append(f"status: {data['status']}")
        if data.get("owner_agent"):
            lines.append(f"owner_agent: {data['owner_agent']}")
        if data.get("write_scope"):
            lines.append(f"write_scope: {_format_handoff_scope(data['write_scope'])}")
        if data.get("executor_used"):
            lines.append(f"executor_used: {data['executor_used']}")
        if data.get("model_used"):
            lines.append(f"model_used: {data['model_used']}")
        if data.get("result_summary"):
            lines.append(f"result_summary: {data['result_summary']}")
        if data.get("verification_summary"):
            lines.append(f"verification_summary: {data['verification_summary']}")
        if data.get("acted_by"):
            lines.append(f"acted_by: {data['acted_by']}")
        if data.get("reason"):
            lines.append(f"reason: {data['reason']}")
        return "\n".join(lines)

    elif name == "resume_handoff":
        data = await _post(api_base, "/models/handoff/resume", args)
        lines = [f"Resumed handoff {data['memory_id']}"]
        lines.append(f"status: {data['status']}")
        lines.append(f"refreshed: {'true' if data.get('refreshed') else 'false'}")
        if data.get("owner_agent"):
            lines.append(f"owner_agent: {data['owner_agent']}")
        if data.get("write_scope"):
            lines.append(f"write_scope: {_format_handoff_scope(data['write_scope'])}")
        if data.get("executor_used"):
            lines.append(f"executor_used: {data['executor_used']}")
        if data.get("model_used"):
            lines.append(f"model_used: {data['model_used']}")
        if data.get("acted_by"):
            lines.append(f"acted_by: {data['acted_by']}")
        if data.get("reason"):
            lines.append(f"reason: {data['reason']}")
        if data.get("project_id"):
            lines.append(f"project_id: {data['project_id']}")
        if data.get("phase"):
            lines.append(f"phase: {data['phase']}")
        if data.get("priority"):
            lines.append(f"priority: {data['priority']}")
        if data.get("task_description"):
            lines.append(f"task: {data['task_description']}")
        if data.get("phase_objective"):
            lines.append(f"phase_objective: {data['phase_objective']}")
        if data.get("definition_of_done"):
            lines.append(f"definition_of_done: {data['definition_of_done']}")
        if data.get("expected_output_shape"):
            lines.append(f"expected_output_shape: {data['expected_output_shape']}")
        if data.get("project_context_summary"):
            lines.append(f"project_context_summary: {data['project_context_summary']}")
        refs = data.get("project_context_refs") or {}
        if refs:
            lines.append("project_context_refs: " + _summarize_handoff_ref_counts(refs))
        return "\n".join(lines)

    elif name == "route_task":
        data = await _post(api_base, "/router/decide", args)
        tier_icon = {"skill": "⚡", "local": "🏠", "cloud": "☁️", "reference": "📞"}.get(data["tier"], "?")
        alts = ", ".join(f"{a['component']}({a['score']:.2f})" for a in data.get("alternatives", []))
        fallbacks = data.get("cloud_fallbacks", [])
        extra_str = ""
        if fallbacks and data["tier"] == "cloud":
            extra_str = "\nCloud fallbacks: " + ", ".join(f"{f['model_id']}({f['score']:.2f})" for f in fallbacks)
        references = data.get("references", [])
        if references and data["tier"] == "reference":
            ref_lines = "\n".join(
                f"  - {r['name']}: {r.get('description','')[:80]}"
                + (f"  → {r['reference_url']}" if r.get("reference_url") else "")
                for r in references
            )
            extra_str = f"\nReferences (pinned resources):\n{ref_lines}"
        return (
            f"{tier_icon} Route to: {data['component']} (tier={data['tier']}, score={data['score']:.2f})\n"
            f"Task type: {data['task_type']}\n"
            f"Reasoning: {data['reasoning']}\n"
            f"Alternatives: {alts or 'none'}"
            f"{extra_str}"
        )

    elif name == "track_task":
        data = await _post(api_base, "/tracker/record", args)
        status = "✓" if args.get("success") else "✗"
        note = f" → corrected to '{data['corrected_task_type']}'" if data.get("corrected_task_type") else ""
        return f"{status} Tracked {data['component']} / {data['task_type']}{note} (event #{data['event_id']})"

    elif name == "tracker_stats":
        params = []
        if args.get("component"):
            params.append(f"component={args['component']}")
        if args.get("task_type"):
            params.append(f"task_type={args['task_type']}")
        if args.get("since_hours"):
            params.append(f"since_hours={args['since_hours']}")
        qs = "?" + "&".join(params) if params else ""
        rows = await _get(api_base, f"/tracker/stats{qs}")
        if not rows:
            return "No performance data yet."
        lines = []
        for r in rows:
            bar = "█" * int(r["success_rate"] * 10)
            lat = f" {r['avg_latency_ms']:.0f}ms" if r["avg_latency_ms"] else ""
            lines.append(f"{r['component']:20s} / {r['task_type']:25s} {bar} {r['success_rate']:.2f}  ({r['success']}✓/{r['fail']}✗){lat}")
        return "\n".join(lines)

    elif name == "skill_search":
        params = []
        if args.get("context"):
            params.append(f"context={args['context']}")
        if args.get("domains"):
            params.append(f"domains={args['domains']}")
        if args.get("platform"):
            params.append(f"platform={args['platform']}")
        params.append(f"limit={args.get('limit', 10)}")
        params.append(f"min_relevance={args.get('min_relevance', 0.3)}")
        results = await _get(api_base, f"/skills/search?{'&'.join(params)}")
        if not results:
            return "No matching skills found."
        lines = []
        for i, s in enumerate(results, 1):
            tags = ", ".join(s.get("domain_tags", []))
            lines.append(f"{i}. [{s['platform']}] **{s['name']}** — {s['description']}\n   domains: {tags}\n   id: {s['id']}\n   install: {s['install_path']}")
        return "\n\n".join(lines)

    elif name == "skill_publish":
        data = await _post(api_base, "/skills/publish", args)
        return f"Published skill '{data['name']}'\nDomain tags: {data['domain_tags']}\nid: {data['id']}"

    elif name == "skill_install":
        data = await _get(api_base, f"/skills/{args['skill_id']}/content")
        return f"Skill: {data['name']}\nInstall to: {data['install_path']}\n\n--- SKILL.md ---\n{data['content']}"

    elif name == "get_artifact":
        artifact_key = args["artifact_key"]
        data = await _get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "list_artifacts":
        query = build_list_artifacts_query(args)
        data = await _get(api_base, f"/artifacts?{query}")
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "list_open_tasks":
        query = build_list_open_tasks_query(args)
        data = await _get(api_base, f"/artifacts?{query}")
        return format_list_open_tasks_response(data)
    elif name == "normalize_mcp_intent":
        payload = build_normalize_mcp_intent_payload(args)
        data = _normalize_mcp_intent(payload["intent"], project_id=payload["project_id"], top_n=payload["top_n"])
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "project_workflow":
        data = build_project_workflow_payload(args)
        data = _annotate_structured_tool_payload(name, data)
        return format_project_workflow_response(data)

    elif name == "project_workflow_submit":
        payload = build_project_workflow_submit_payload(args)
        plan = build_project_workflow_submit_plan(payload)
        if plan["status"] != "ready":
            return format_project_workflow_submit_response(plan)
        receipt = await _execute_mcp_operation_plan(api_base, plan)
        return format_project_workflow_submit_response(receipt)

    elif name == "continue_task":
        data = await _build_continue_task_payload(api_base, args)
        data = _annotate_structured_tool_payload(name, data)
        return format_continue_task_response(data)

    elif name in {"report_task_checkpoint", "record_task_checkpoint"}:
        from app.services.mcp_session_store import get_session_store

        payload = build_report_task_checkpoint_payload(args)
        task_id = str(args["task_id"]).strip()
        stage = str(args["stage"]).strip().lower()
        status_tag = next((tag for tag in payload.get("tags", []) if str(tag).startswith("task_status:")), "")
        status = str(args.get("status") or "").strip().lower()
        if not status and isinstance(status_tag, str) and ":" in status_tag:
            status = status_tag.split(":", 1)[1]
        if not status:
            status = "active"
        if session_id:
            try:
                await get_session_store().patch_context(
                    session_id,
                    {
                        "current_task_checkpoint": {
                            "project": str(args["project"]).strip(),
                            "task_id": task_id,
                            "stage": stage,
                            "status": status,
                            "summary": str(args["summary"]).strip(),
                            "blockers": [str(item).strip() for item in (args.get("blockers") or []) if str(item).strip()],
                            "next_step": str(args.get("next_step") or "").strip(),
                            "reason": str(args.get("reason") or "").strip(),
                        },
                        "task_checkpoint_recorded": False,
                    },
                )
            except Exception:
                pass
        data = await _post(api_base, f"/project/tasks/{quote(task_id, safe='')}/changes", payload)
        handoff_data = None
        handoff_error = None
        if name == "record_task_checkpoint" and stage in _CHECKPOINT_HANDOFF_STAGES:
            try:
                handoff_data = await _post(api_base, "/models/handoff", _checkpoint_handoff_payload(args, stage=stage, status=status))
            except Exception as exc:
                handoff_error = str(exc)
        if session_id:
            try:
                await get_session_store().patch_context(
                    session_id,
                    {
                        "current_task_checkpoint": {
                            "project": str(args["project"]).strip(),
                            "task_id": task_id,
                            "stage": stage,
                            "status": status,
                            "summary": str(args["summary"]).strip(),
                            "blockers": [str(item).strip() for item in (args.get("blockers") or []) if str(item).strip()],
                            "next_step": str(args.get("next_step") or "").strip(),
                            "reason": str(args.get("reason") or "").strip(),
                            "recorded_at": time.time(),
                        },
                        "task_checkpoint_recorded": True,
                        "task_checkpoint_recorded_at": time.time(),
                    },
                )
            except Exception:
                pass
        data["task_id"] = task_id
        data["stage"] = stage
        data["status"] = status
        if handoff_data:
            data["handoff_packet_created"] = True
            data["handoff_memory_id"] = handoff_data.get("memory_id")
            data["handoff_label"] = handoff_data.get("handoff_label")
        elif name == "record_task_checkpoint":
            data["handoff_packet_created"] = False
            if handoff_error:
                data["handoff_error"] = handoff_error
        return format_task_checkpoint_response(data)

    elif name == "reopen_task":
        task_id = str(args["task_id"]).strip()
        payload = build_reopen_task_payload(args)
        data = await _post(api_base, f"/project/tasks/{quote(task_id, safe='')}/reopen", payload)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "list_tool_families":
        data = _build_tool_families_payload(
            include_compatibility_note=bool(args.get("include_compatibility_note", True)),
        )
        data = _annotate_structured_tool_payload(name, data)
        return format_list_tool_families_response(data)

    elif name == "tool_family_tools":
        family = str(args["family"]).strip()
        depth = str(args.get("depth", "brief")).strip() or "brief"
        data = _build_family_tools_payload(
            family,
            depth=depth,
            limit=int(args.get("limit", 12)),
        )
        data = _annotate_structured_tool_payload(name, data)
        return format_tool_family_tools_response(data)

    elif name == "tool_explain":
        tool_name = str(args["tool_name"]).strip()
        task_context = str(args.get("task_context") or "").strip()
        data = _build_tool_explanation(tool_name, task_context=task_context)
        data = _annotate_structured_tool_payload(name, data)
        return format_tool_explain_response(data)

    elif name == "tool_recommend":
        task = str(args["task"]).strip()
        project_id = str(args.get("project_id") or "").strip()
        top_n = int(args.get("top_n", 3))
        data = _build_tool_recommendation(task, project_id=project_id, top_n=top_n)
        if project_id:
            try:
                project_bundle = await _post(
                    api_base,
                    "/project/enrich-task",
                    {
                        "project_id": project_id,
                        "task": task,
                        "max_components": 3,
                    },
                )
                project_calls = project_bundle.get("recommended_mcp_calls") or []
                if project_calls:
                    data["project_recommended_calls"] = project_calls[:top_n]
                    data["project_context_summary"] = str(project_bundle.get("context") or "").strip()[:1200]
            except Exception:
                pass
        data = _annotate_structured_tool_payload(name, data)
        return format_tool_recommend_response(data)

    elif name == "tool_feedback":
        from app.services.learning_store import get_learning_store

        tool_name = str(args["tool_name"]).strip()
        tool_stage = str(args.get("tool_stage") or get_tool_stage(tool_name)).strip() or "testing"
        valence = str(args["valence"]).strip().lower()
        worked = bool(args.get("worked", valence == "positive"))
        scope = str(args.get("scope") or "").strip()
        what_was_tested = str(args.get("what_was_tested") or "").strip()
        expected_behavior = str(args.get("expected_behavior") or "").strip()
        observed_behavior = str(args.get("observed_behavior") or "").strip()
        friction = str(args.get("friction") or "").strip()
        suggestion = str(args.get("suggestion") or "").strip()
        next_action = str(args.get("next_action") or "").strip()
        assessment = str(args.get("assessment") or "").strip()
        task_context = str(args.get("task_context") or "").strip()
        missing_fields = args.get("missing_fields") or []
        if isinstance(missing_fields, str):
            missing_fields = [missing_fields]
        payload = {
            "tool_name": tool_name,
            "tool_stage": tool_stage,
            "project_id": str(args.get("project_id") or "").strip(),
            "task_context": task_context,
            "friction": friction,
            "suggestion": suggestion,
            "missing_fields": [str(item).strip() for item in missing_fields if str(item).strip()],
            "worked": worked,
            "agent_id": str(args.get("agent_id") or "mcp-agent").strip() or "mcp-agent",
            "session_id": str(args.get("session_id") or session_id or "").strip(),
        }
        valence_for_store = "positive" if worked and valence != "negative" else "negative"
        magnitude = 0.9 if valence_for_store == "positive" else 0.4
        store = get_learning_store()
        feedback_id = await store.write_feedback(
            valence=valence_for_store,
            episode_id=payload["session_id"],
            magnitude=magnitude,
            source="mcp_tool_feedback",
            payload=payload,
        )
        try:
            record_tool_feedback(
                tool_name=tool_name,
                valence=valence_for_store,
                tool_stage=tool_stage,
                worked=worked,
                friction=friction,
                suggestion=suggestion,
                task_context=task_context,
                project_id=payload["project_id"],
                agent_id=payload["agent_id"],
                session_id=payload["session_id"],
                missing_fields=payload["missing_fields"],
            )
        except Exception:
            pass
        try:
            await store.write_event(
                event_type="artifact_feedback",
                agent_id=payload["agent_id"],
                project=payload["project_id"],
                transport="mcp",
                episode_id=payload["session_id"],
                context_signature=f"tool={tool_name};stage={tool_stage};transport=mcp",
                payload={
                    "tool_name": tool_name,
                    "tool_stage": tool_stage,
                    "valence": valence_for_store,
                    "worked": worked,
                    "friction": friction,
                    "suggestion": suggestion,
                "missing_fields": payload["missing_fields"],
                "task_context": task_context,
            },
        )
        except Exception:
            pass
        data = _build_tool_feedback_envelope(
            tool_name=tool_name,
            tool_stage=tool_stage,
            valence=valence_for_store,
            worked=worked,
            friction=friction,
            suggestion=suggestion,
            task_context=task_context,
            project_id=payload["project_id"],
            agent_id=payload["agent_id"],
            session_id=payload["session_id"],
            missing_fields=payload["missing_fields"],
            feedback_id=feedback_id,
            assessment=assessment or None,
            scope=scope,
            what_was_tested=what_was_tested,
            expected_behavior=expected_behavior,
            observed_behavior=observed_behavior,
            next_action=next_action,
        )
        return format_tool_feedback_response(data)

    elif name == "resolve_artifact":
        artifact_key = args["artifact_key"]
        payload = {
            "acted_by": args.get("acted_by", "user"),
            "action_source": args.get("action_source", "inline_user_approval"),
            "reason": args.get("reason", ""),
        }
        data = await _post(api_base, f"/artifacts/{quote(artifact_key, safe='')}/resolve", payload)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "reopen_artifact":
        artifact_key = args["artifact_key"]
        project = args.get("project")
        if not project and ":" in artifact_key:
            project = artifact_key.split(":", 2)[1]
        payload = {
            "project": project,
            "status": args.get("status", "active"),
            "reason": args.get("reason", "reopen_artifact"),
            "acted_by": args.get("acted_by", "user"),
            "action_source": args.get("action_source", "unified_artifact"),
            "source": args.get("source", "unified-artifact"),
        }
        data = await _post(api_base, f"/artifacts/{quote(artifact_key, safe='')}/reopen", payload)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "memory_health":
        data = await _get(api_base, "/health")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "ingest_file":
        data = await _post(api_base, "/ingest/file", args)
        return f"File ingested: inserted={data['inserted']} failed={data['failed']} skipped={data['skipped']}"

    elif name == "ingest_dir":
        data = await _post(api_base, "/ingest/dir", args)
        return (
            f"Directory ingested: files={data['files_processed']} "
            f"inserted={data['inserted']} failed={data['failed']} skipped={data['skipped']}"
        )

    elif name == "get_onboarding":
        from app.services.instruction_layers import (
            build_layered_onboarding,
            infer_instruction_category,
        )
        
        agent_id = args.get("agent_id", "default")
        task_desc = args.get("task_description", "")

        # Get tools called from session context for category inference
        tools_called = []
        if session_id:
            from app.services.mcp_session_store import get_session_store
            ctx = await get_session_store().get_context(session_id)
            if ctx:
                tools_called = [t["tool"] for t in ctx.get("tools_called", [])]
        
        # 1. Determine domains: from task_description OR infer from history ("lost child" case)
        # This is the SINGLE source of truth for domains used by both L2 and skill pack
        domains: list[str] = []
        inferred = False
        try:
            if task_desc:
                profile = await _post(api_base, "/skills/profile", {"text": task_desc})
                domains = profile.get("domains", []) or []
            else:
                # Agent said "I'm lost" — infer domain from history
                session_hints = []
                if session_id:
                    from app.services.mcp_session_store import get_session_store
                    _ctx = await get_session_store().get_context(session_id)
                    if _ctx:
                        session_hints = [t["tool"] for t in _ctx.get("tools_called", [])]
                inference = await _post(api_base, "/skills/infer-domain", {
                    "agent_id": agent_id, "session_hints": session_hints,
                })
                domains = inference.get("all_domains", []) or []
                top_domain = inference.get("domain", "general")
                confidence = inference.get("confidence", 0.0)
                signals = inference.get("signals", [])
                inferred = True
        except Exception as e:
            domains = ["general"]

        if not domains:
            domains = ["general"]
        
        # Build layered instructions (L0, L1, L2) using the single domains source
        layered_content = build_layered_onboarding(
            task_description=task_desc,
            priority="normal",  # Could be extracted from handoff context
            phase="",  # Could be extracted from handoff context
            next_steps=None,  # Could be extracted from handoff context
            tools_called=tools_called,
            domains=domains,
            include_l2=True,
        )
        
        sections: list[str] = [layered_content]
        pack_id = ""
        
        # Add orientation message if domains were inferred from history
        if inferred and top_domain != "general":
            sections.append(
                f"ORIENTATION (inferred from your history):\n"
                f"  You appear to be working in: {top_domain} (confidence: {confidence:.0%})\n"
                f"  Evidence: {'; '.join(signals[:3])}"
            )
        elif inferred:
            sections.append(
                "ORIENTATION: No prior history found — you are a new agent.\n"
                "  Call record_outcome after your session to help future agents."
            )

        try:
            storage_trust = await _get(api_base, "/admin/storage-trust")
            trust_status = storage_trust.get("status", "unknown")
            if trust_status != "ok":
                trust_summary = storage_trust.get("summary") or "Storage trust is not fully healthy."
                sections.append(
                    "STORAGE TRUST WARNING:\n"
                    f"  Status: {trust_status}\n"
                    f"  {trust_summary}\n"
                    "  Call get_storage_trust_status for current degraded slices, hygiene findings, and next actions."
                )
        except Exception:
            pass

        sections.append(
            render_onboarding_instincts_block(
                get_active_operational_instincts(
                    context_type="onboarding",
                    storage_trust_status=locals().get("trust_status", "ok"),
                    limit=5,
                )
            )
        )

        # 0. Always prepend pinned references (the "phone on the wall")
        try:
            pinned = await _get(api_base, "/skills/pinned")
            if isinstance(pinned, list) and pinned:
                ref_skills = [s for s in pinned if s.get("reference_url")]
                plain_pinned = [s for s in pinned if not s.get("reference_url")]
                if ref_skills:
                    ref_lines = "\n".join(
                        f"  - {s['name']}: {s.get('description','')[:80]}  → {s['reference_url']}"
                        for s in ref_skills
                    )
                    sections.append(f"EMERGENCY REFERENCES (always available):\n{ref_lines}")
                if plain_pinned:
                    skill_lines = "\n".join(f"  - {s['name']}: {s.get('description','')[:80]}" for s in plain_pinned)
                    sections.append(f"PINNED SKILLS (always relevant):\n{skill_lines}")
        except Exception:
            pass

        # 0.5 Behavioral patterns ready for automation (conditional reflexes)
        try:
            from app.services.behavior_adaptation import iter_behavior_evals

            automatable = [
                p for p in iter_behavior_evals(agent_id)
                if p.suggest_automation and not p.high_risk
            ]
            if automatable:
                lines = []
                for p in automatable[:5]:
                    ctx = f" ({p.context_signature})" if p.context_signature else ""
                    lines.append(
                        f"  - {p.action_type}{ctx}: confidence={p.confidence} accepts={p.accepts}"
                    )
                sections.append(
                    "AUTOMATABLE HABITS (conditional reflexes):\n"
                    + "\n".join(lines)
                    + "\n\n"
                    "TIP: You may start doing these automatically. If the user opts out, "
                    "call POST /skills/behavior/patterns/{action_type}/reset."
                )
        except Exception:
            pass

        # 2. Skill pack for inferred/provided domains
        try:
            pack = await _post(api_base, "/skills/pack/create", {
                "domains": domains, "task_type": "onboarding",
                "agent_id": agent_id, "confidence": 0.6, "limit": 5,
            })
            pack_id = pack.get("pack_id", "")
            skills = pack.get("skills", [])
            if session_id:
                from app.services.mcp_session_store import get_session_store
                await get_session_store().patch_context(session_id, {
                    "pack_id": pack_id,
                    "skills_received": [s.get("id", "") for s in skills],
                })

            label = "SKILLS FOR YOUR SESSION" if not inferred else f"SKILLS FOR DOMAIN '{domains[0]}'"
            if pack.get("degraded"):
                reason = pack.get("degraded_reason") or "Skill retrieval is running in degraded mode."
                sections.append(f"INTEGRITY WARNING: {reason}")
            if skills:
                skill_lines = "\n".join(f"  - {s['name']}: {s.get('description','')[:80]}" for s in skills)
                sections.append(f"{label} (pack_id={pack_id}):\n{skill_lines}")
            else:
                sections.append("No specific skills found yet — contribute outcomes to improve this.")
        except Exception as e:
            reason = _format_tool_error_brief(e, default="skill pack retrieval failed")
            sections.append(
                f"Skills: temporarily unavailable ({reason}). "
                "Continue with onboarding basics and retry later."
            )

        # 3. Domain gaps from collective experience
        try:
            gaps_data = await _get(api_base, f"/skills/gaps?agent_id={agent_id}&min_count=1")
            gaps = gaps_data.get("gaps", [])
            if gaps:
                gap_lines = ", ".join(g["domain"] for g in gaps[:5])
                sections.append(f"KNOWN KNOWLEDGE GAPS (from past sessions): {gap_lines}")
        except Exception:
            pass

        # 4. Analytics summary
        try:
            analytics = await _get(api_base, f"/skills/analytics?agent_id={agent_id}")
            total = analytics.get("total_outcomes", 0)
            rate = analytics.get("success_rate")
            if total > 0:
                sections.append(
                    f"COLLECTIVE EXPERIENCE: {total} past sessions, "
                    f"{int((rate or 0)*100)}% success rate"
                )
        except Exception:
            pass

        # 5. Recent memories for this agent type
        try:
            recent = await _get(api_base, f"/memories/recent?agent_id={agent_id}&limit=3&minutes=10080")
            if isinstance(recent, list) and recent:
                mem_lines = "\n".join(f"  - {m['content'][:100]}" for m in recent[:3])
                sections.append(f"RECENT CONTEXT FOR YOUR AGENT:\n{mem_lines}")
        except Exception:
            pass

        sections.append(
            "TIP: Call record_outcome at the end of your session to teach the system. "
            f"Use pack_id={pack_id!r} to reference this session's skill pack."
        )
        return "\n\n".join(sections)

    elif name == "record_outcome":
        data = await _post(api_base, "/skills/outcome", {
            "pack_id": args.get("pack_id", "manual"),
            "agent_id": args.get("agent_id", "default"),
            "skills_helpful": args.get("skills_helpful", []),
            "skills_unused": args.get("skills_unused", []),
            "missing_domains": args.get("missing_domains", []),
            "success": args.get("success", True),
        })
        return (
            f"Outcome recorded. Thank you — this improves onboarding for future agents.\n"
            f"report_id={data.get('report_id', '?')} success={data.get('stats', {}).get('success')}"
        )

    elif name == "load_instruction_layer":
        from app.services.instruction_layers import (
            get_l3_layer,
            get_l4_layer,
        )
        layer = args.get("layer", "L3")
        if layer == "L3":
            category = args.get("category", "memory_operations")
            section = args.get("section", "api_reference")
            content = get_l3_layer(category, section)
        elif layer == "L4":
            section = args.get("section", "advanced_patterns")
            content = get_l4_layer(section)
        else:
            return f"Invalid layer: {layer}. Use 'L3' or 'L4'."
        return format_load_instruction_layer_response(content)

    elif name == "list_instruction_layers":
        from app.services.instruction_layers import list_available_layers
        payload = build_list_instruction_layers_payload(args)
        layers = list_available_layers(payload.get("layer"))
        return format_list_instruction_layers_response(layers)

    elif name == "search_project_knowledge":
        data = await _post(api_base, "/project/search", {
            "project_id": args["project_id"],
            "query": args["query"],
            "limit": args.get("limit", 5),
        })
        results = data.get("results", [])
        if not results:
            return (
                f"No components found for query '{args['query']}' in project '{args['project_id']}'.\n"
                "Run POST /project/ingest first to index the project."
            )
        lines = [f"Project '{args['project_id']}' — {len(results)} component(s) found:\n"]
        for r in results:
            lines.append(f"### {r['name']} ({r['component_id']})  score={r['score']}")
            lines.append(f"Purpose: {r['purpose']}")
            lines.append(f"Implementation: {r['implementation']}")
            if r.get("endpoints"):
                lines.append(f"Endpoints: {', '.join(r['endpoints'])}")
            if r.get("key_files"):
                lines.append(f"Key files: {', '.join(r['key_files'])}")
            if r.get("version_note"):
                lines.append(f"Note: {r['version_note']}")
            lines.append("")
        return "\n".join(lines)

    elif name == "enrich_task_with_context":
        data = await _post(api_base, "/project/enrich-task", build_enrich_task_payload(args))
        return format_enrich_task_response(data)
    elif name == "get_project_readiness":
        data = await _post(api_base, "/project/readiness", build_project_readiness_payload(args))
        return format_project_readiness_response(data)
    elif name == "get_project_bootstrap_checklist":
        data = await _post(api_base, "/project/bootstrap-checklist", build_project_bootstrap_payload(args))
        return format_project_bootstrap_response(data)
    elif name == "plan_remote_snapshot":
        data = await _post(api_base, "/project/remote-snapshot/plan", build_remote_snapshot_payload(args))
        return format_remote_snapshot_plan_response(data)
    elif name == "sync_remote_snapshot":
        data = await _post(api_base, "/project/remote-snapshot/sync", build_remote_snapshot_payload(args))
        return format_remote_snapshot_sync_response(data)
    elif name == "get_storage_trust_status":
        data = await _get(api_base, "/admin/storage-trust")
        return format_storage_trust_response(data)

    elif name == "send_coordination_message":
        data = await _post(api_base, "/models/coordination/messages", build_send_coordination_message_payload(args))
        return format_coordination_message(data, prefix="Sent coordination message")
    elif name == "pickup_coordination_messages":
        data = await _post(api_base, "/models/coordination/pickup", build_pickup_coordination_payload(args))
        return format_coordination_list(data, empty_text=f"No new coordination messages for agent '{args['agent_id']}'.")
    elif name == "list_coordination_messages":
        data = await _get(api_base, f"/models/coordination/messages?{build_list_coordination_query(args)}")
        return format_coordination_list(data, empty_text="No coordination messages matched the query.")
    elif name == "update_coordination_message_status":
        data = await _post(
            api_base,
            f"/models/coordination/messages/{args['message_id']}/status",
            build_coordination_status_payload(args),
        )
        return format_coordination_message(data, prefix="Updated coordination message")

    elif name == "get_task_status":
        data = await _get(api_base, f"/tasks/{args['job_id']}")
        status = data.get("status", "unknown")
        job_type = data.get("job_type", "")
        lines = [f"Job {args['job_id'][:8]}… | type={job_type} | status={status}"]
        if status == "done":
            result = data.get("result") or {}
            lines.append(f"Result: {result}")
        elif status == "failed":
            lines.append(f"Error: {data.get('error', 'unknown error')}")
        elif status == "running":
            started = data.get("started_at")
            lines.append(f"Started at: {started}")
        return "\n".join(lines)

    else:
        raise ValueError(f"Unknown tool: {name}")


# ── JSON-RPC handler ───────────────────────────────────────────────────────────

def _ok(req_id: Any, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def _err(req_id: Any, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Error: {msg}"}], "isError": True}}


async def _auto_record_session(ctx: dict) -> None:
    """Auto-record a passive session observation when an SSE connection closes."""
    try:
        api_base = ctx.get("api_base", "")
        agent_id = ctx.get("agent_id", "default")
        pack_id = ctx.get("pack_id") or "auto"
        tools_called = {t["tool"] for t in ctx.get("tools_called", [])}
        skills_received = ctx.get("skills_received", [])
        checkpoint = ctx.get("current_task_checkpoint") or {}

        # Infer which received skills were actually used (agent called related tools)
        used_tools = {"memory_store", "memory_search", "memory_context", "record_memory_outcome",
                      "ingest_file", "ingest_dir", "skill_search", "skill_install",
                      "crystallize_solution", "knowledge_hierarchy", "canonicals_by_scope",
                      "list_project_laws", "get_project_law",
                      "set_canonical_status", "merge_canonicals"}
        was_active = bool(tools_called & used_tools)

        # Skills that were received but agent didn't use any productive tools → unused
        skills_helpful = skills_received if was_active else []
        skills_unused = skills_received if not was_active else []

        duration_s = time.time() - ctx.get("connected_at", time.time())

        if checkpoint and not ctx.get("task_checkpoint_recorded"):
            project = str(checkpoint.get("project") or "").strip()
            task_id = str(checkpoint.get("task_id") or "").strip()
            stage = str(checkpoint.get("stage") or "").strip().lower()
            status = str(checkpoint.get("status") or "").strip().lower()
            summary = str(checkpoint.get("summary") or "").strip()
            blockers = checkpoint.get("blockers") or []
            next_step = str(checkpoint.get("next_step") or "").strip()
            reason = str(checkpoint.get("reason") or "").strip()
            if project and task_id and stage and summary:
                try:
                    await _post(api_base, f"/project/tasks/{quote(task_id, safe='')}/changes", build_report_task_checkpoint_payload({
                        "project": project,
                        "task_id": task_id,
                        "stage": stage,
                        "status": status or None,
                        "summary": summary,
                        "blockers": blockers,
                        "next_step": next_step,
                        "reason": reason or "session_closed_auto_checkpoint",
                        "acted_by": agent_id,
                        "source": "mcp_session_close",
                    }))
                except Exception:
                    pass

        await _post(api_base, "/skills/outcome", {
            "pack_id": pack_id,
            "agent_id": agent_id,
            "skills_helpful": skills_helpful,
            "skills_unused": skills_unused,
            "missing_domains": [],
            "success": was_active,
        })

        # Also store session summary as a memory for future cross-agent recall
        query_summary = "; ".join(ctx.get("queries", [])[:5])
        if query_summary:
            await _post(api_base, "/memories", {
                "content": (
                    f"Agent {agent_id} session summary: "
                    f"searched for [{query_summary}], "
                    f"used {len(tools_called)} tools over {int(duration_s)}s"
                ),
                "agent_id": agent_id,
                "memory_type": "experience",
                "category": "session_observation",
                "importance_score": 0.5,
                "source": "auto-session-observer",
                "tags": ["session_observation", f"agent:{agent_id}"],
            })

        # Feed dialogue analyzer with session-level human text captured from tool args.
        snippets = ctx.get("dialogue_snippets") or []
        if isinstance(snippets, list):
            transcript = _build_dialogue_transcript([s for s in snippets if isinstance(s, dict)])
            if len(transcript.strip()) >= 60:
                await _post(api_base, "/skills/dialogue/analyze", {
                    "transcript": transcript,
                    "agent_id": agent_id,
                    "session_id": ctx.get("session_id") or "",
                })
    except Exception:
        pass  # Never let observer errors surface


async def _handle(msg: dict, api_base: str, session_id: str | None = None) -> dict | None:
    method = msg.get("method", "")
    req_id = msg.get("id")

    if method == "initialize":
        # Extract agent identity from clientInfo
        client_info = msg.get("params", {}).get("clientInfo", {})
        agent_name = client_info.get("name", "") or ""
        # Normalise: "Claude Code" → "claude-code", "Codex CLI" → "codex"
        agent_id = agent_name.lower().replace(" ", "-") if agent_name else None

        if session_id and agent_id:
            from app.services.mcp_session_store import get_session_store
            await get_session_store().set_context(session_id, {
                "agent_id": agent_id,
                "connected_at": time.time(),
                "api_base": api_base,
                "tools_called": [],
                "queries": [],
                "skills_accessed": [],
                "skills_received": [],
                "pack_id": None,
                "session_id": session_id,
                "dialogue_snippets": [],
            })

        result: dict = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "super-memory", "version": "1.0.0"},
        }
        if agent_id:
            result["_supermemory"] = build_supermemory_initialize_hint(agent_id)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    elif method in ("initialized", "notifications/initialized"):
        return None  # notification — no response

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    elif method == "tools/call":
        params = msg.get("params", {})
        try:
            result_text = await _execute_tool(
                params.get("name", ""), params.get("arguments", {}), api_base, session_id
            )
            return _ok(req_id, result_text)
        except httpx.HTTPStatusError as e:
            return _err(req_id, f"HTTP {e.response.status_code}: {e.response.text[:500]}")
        except httpx.RequestError as e:
            return _err(req_id, f"Cannot connect to memory server: {e}")
        except Exception as e:
            return _err(req_id, str(e))

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    else:
        if req_id is not None:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return None


# ── Streamable HTTP endpoint (MCP 2025-03-26, used by Codex CLI) ───────────────

@router.post("/sse")
async def streamable_http(request: Request):
    """MCP Streamable HTTP transport — accepts JSON-RPC, returns JSON directly."""
    base = str(request.base_url).rstrip("/")
    api_base = f"{base}/api/v1"

    body = await request.json()

    # Batch request (array of JSON-RPC objects)
    if isinstance(body, list):
        results = []
        for msg in body:
            r = await _handle(msg, api_base)
            if r is not None:
                results.append(r)
        return Response(
            content=json.dumps(results, ensure_ascii=False),
            media_type="application/json",
        )

    # Single request
    result = await _handle(body, api_base)
    if result is None:
        return Response(status_code=202)

    return Response(
        content=json.dumps(result, ensure_ascii=False),
        media_type="application/json",
    )


# ── SSE endpoints ──────────────────────────────────────────────────────────────

@router.get("/sse")
async def sse_connect(request: Request) -> StreamingResponse:
    """Open SSE stream. Server sends endpoint URL, then streams JSON-RPC responses."""
    _ensure_cleanup_task()
    await _evict_expired_sessions()
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
    _SESSIONS[session_id] = queue
    from app.services.mcp_session_store import get_session_store
    await get_session_store().init_session(session_id)

    # Build the POST endpoint URL using the same host/scheme the client used
    base = str(request.base_url).rstrip("/")
    endpoint = f"{base}/mcp/messages?sessionId={session_id}"

    async def stream():
        try:
            # Step 1: tell the client where to POST requests
            yield f"event: endpoint\ndata: {endpoint}\n\n"

            # Step 2: relay responses back over the stream
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    if msg is None:
                        break
                    yield f"event: message\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    await _touch_session(session_id)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # prevent proxy from closing idle connections
        finally:
            _SESSIONS.pop(session_id, None)
            from app.services.mcp_session_store import get_session_store
            ctx = await get_session_store().close_session(session_id)
            if ctx and ctx.get("tools_called"):
                # Auto-record passive session observation
                asyncio.create_task(_auto_record_session(ctx))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/messages")
async def sse_post(sessionId: str, request: Request) -> Response:
    """Receive a JSON-RPC message from the client and push the response to the SSE stream."""
    _ensure_cleanup_task()
    await _evict_expired_sessions()
    queue = _SESSIONS.get(sessionId)
    if queue is None:
        raise HTTPException(status_code=404, detail=f"Session {sessionId!r} not found or expired")

    # Build api_base so tools call back to this same server
    base = str(request.base_url).rstrip("/")
    api_base = f"{base}/api/v1"

    body = await request.json()
    await _touch_session(sessionId)
    result = await _handle(body, api_base, session_id=sessionId)
    if result is not None:
        await _queue_put(queue, result)
        await _touch_session(sessionId)

    return Response(status_code=202)
