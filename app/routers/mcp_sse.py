"""
MCP SSE Transport for FastAPI (spec 2024-11-05).

Allows zero-config client connection — no Python needed on the client:

    claude mcp add --transport sse -s user mnemoforge http://<SERVER_IP>:8000/mcp/sse

Protocol:
  GET  /mcp/sse                      — open SSE stream, receive endpoint URL
  POST /mcp/messages?sessionId=<id>  — send JSON-RPC requests
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
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
    build_enrich_task_payload,
    build_operational_tray_context_payload,
    build_upsert_knowledge_tree_node_payload,
    build_task_execution_context_payload,
    build_list_open_tasks_query,
    build_normalize_mcp_intent_payload,
    build_project_workflow_payload,
    build_project_workflow_submit_payload,
    build_project_workflow_submit_plan,
    build_project_bootstrap_payload,
    build_project_reconstruction_payload,
    build_project_readiness_payload,
    build_remote_snapshot_payload,
    build_reopen_task_payload,
    build_mnemoforge_initialize_hint,
    build_mnemoforge_onboarding_basics,
    build_report_task_checkpoint_payload,
    format_list_open_tasks_response,
    format_task_checkpoint_response,
    format_pull_task_context_response,
    format_enrich_task_response,
    format_project_bootstrap_response,
    format_project_reconstruction_response,
    format_project_workflow_response,
    format_project_workflow_submit_response,
    format_remote_snapshot_plan_response,
    format_remote_snapshot_sync_response,
    format_project_readiness_response,
    format_storage_trust_response,
    sync_tool_definitions,
    tool_definition,
)
from app.services.operational_instincts_service import (
    get_active_operational_instincts,
    render_onboarding_instincts_block,
)
from app.services.mcp_tool_registry import get_tool_stage, observe_tool_use, record_tool_feedback, tool_feedback_expected
from app.services.replay_completeness_service import build_replay_drill_decision, build_token_budget, evaluate_execution_readiness, evaluate_replay_completeness
from app.services.route_pattern_store import get_route_pattern_store
from app.services.mcp_mailbox import mailbox_form_by_id
from app.services.mcp_mailbox_actions import (
    MailboxActionDependencies,
    build_mailbox_submit_packet as build_mailbox_action_submit_packet,
    public_mailbox_error_message,
)
from app.services.mcp_ask_project_actions import (
    ask_project_query_text as _ask_project_query_text,
    ask_project_response_format as _ask_project_response_format,
    select_ask_project_lexical_route,
)
from app.services.mcp_mailbox_read import (
    MailboxReadDependencies,
    build_mailbox_get_response,
    build_mailbox_state_response,
)
from app.services.mcp_simple_read_actions import (
    PublicRefDependencies,
    SimpleReadDependencies,
    build_simple_get_query_response,
    build_simple_public_ref_response,
)
from app.services.mcp_simple_surface_actions import (
    SimpleSurfaceDependencies,
    build_simple_get_response,
    build_simple_help_response,
    build_simple_state_response,
    build_simple_submit_response,
)
from app.services.mcp_project_work_routing import (
    PROJECT_WORK_ROUTE_CATALOG as _PROJECT_WORK_ROUTE_CATALOG,
    project_work_needs_llm_disambiguation as _project_work_needs_llm_disambiguation,
    project_work_route as _project_work_route,
)
from app.services.mcp_project_rules_routing import (
    PROJECT_RULES_ROUTE_CATALOG as _PROJECT_RULES_ROUTE_CATALOG,
    project_rules_route,
)
from app.services.mcp_task_lease_actions import (
    TaskLeaseActionDependencies,
    execute_task_lease_action,
    task_mutation_requires_owned_claim,
)
from app.services.mcp_task_session_actions import (
    TaskSessionActionDependencies,
    finish_task_session_action,
    start_task_session_action,
)
from app.services.mcp_work_session_actions import (
    WorkSessionActionDependencies,
    execute_work_session_action,
)
from app.services.mcp_checkpoint_draft_actions import (
    CheckpointDraftActionDependencies,
    checkpoint_draft_recommended_next_tool,
    execute_checkpoint_draft_action,
)
from app.services.mcp_task_checkpoint_actions import (
    TaskCheckpointActionDependencies,
    checkpoint_handoff_payload,
    checkpoint_scope_guard,
    checkpoint_scope_guard_decision,
    execute_task_checkpoint_action,
)
from app.services.mcp_tool_discovery_actions import (
    ToolDiscoveryActionDependencies,
    build_tool_feedback_envelope,
    execute_tool_discovery_action,
)
from app.services.mcp_artifact_lifecycle_actions import (
    ArtifactLifecycleActionDependencies,
    execute_artifact_lifecycle_action,
)
from app.services.mcp_project_governance_actions import (
    ProjectGovernanceActionDependencies,
    execute_project_governance_action,
)
from app.services.mcp_runtime_utility_actions import (
    RuntimeUtilityActionDependencies,
    execute_runtime_utility_action,
)
from app.services.mcp_grouped_tool_dispatch_actions import (
    GroupedToolDispatchDependencies,
    execute_grouped_memory_or_runtime_action,
)
from app.services.mcp_workflow_specs import load_route_catalog_spec, load_tool_family_registry
from app.services.mcp_handoff_actions import (
    HANDOFF_ACTIONS,
    HandoffActionDependencies,
    execute_handoff_action,
)
from app.services.mcp_skill_routing_actions import (
    SKILL_ROUTING_ACTIONS,
    SkillRoutingActionDependencies,
    execute_skill_routing_action,
)
from app.services.mcp_coordination_actions import (
    COORDINATION_ACTIONS,
    CoordinationActionDependencies,
    execute_coordination_action,
)

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
    family.id: family.model_dump(exclude={"id"})
    for family in load_tool_family_registry().families
}


def _find_tool_definition(tool_name: str) -> dict[str, Any] | None:
    for tool in TOOLS:
        if tool.get("name") == tool_name:
            return tool
    return None


def _normalized_tool_name(tool_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(tool_name or "").strip().lower()).strip("_")


def _unknown_tool_recovery_pattern(name: str, args: dict[str, Any]) -> str:
    parts = [f"tool_name={_normalized_tool_name(name)}"]
    project = str(args.get("project") or args.get("project_id") or "").strip()
    if project:
        parts.append(f"project={project}")
    for key in ("intent", "question", "task", "summary", "raw_notes", "state"):
        value = str(args.get(key) or "").strip()
        if value:
            parts.append(f"{key}={value[:240]}")
    if args:
        parts.append("arg_keys=" + ",".join(sorted(str(key) for key in args.keys())))
    return "\n".join(parts)


def _unknown_tool_call_args(tool_name: str, args: dict[str, Any], *, recovered_text: str = "") -> dict[str, Any]:
    project = str(args.get("project") or args.get("project_id") or "mnemoforge").strip() or "mnemoforge"
    limit = int(args.get("limit", 50))
    text = recovered_text.strip()
    if tool_name == "ask_project":
        return {
            "project": project,
            "question": text or f"Help route this project request that arrived via unknown tool '{_normalized_tool_name(args.get('name') or tool_name)}'.",
            "detail": "full",
            "response_format": "json",
            "client_profile": "agent",
        }
    if tool_name == "project_work":
        return {
            "project": project,
            "intent": text or "Interpret this unknown tool request and choose the right open-work or task route.",
            "allow_mutation": False,
            "detail": "full",
            "response_format": "json",
            "limit": limit,
        }
    if tool_name == "project_context":
        return {
            "project": project,
            "intent": text or "Interpret this unknown tool request and provide the relevant project context.",
            "detail": "full",
            "response_format": "json",
        }
    if tool_name == "project_verify":
        return {
            "project": project,
            "intent": text or "Interpret this unknown tool request and choose the right verification path.",
            "response_format": "json",
        }
    if tool_name == "project_capture":
        return {
            "project": project,
            "intent": text or "Interpret this unknown tool request and choose the right capture or checkpoint path.",
            "allow_mutation": False,
            "response_format": "json",
        }
    if tool_name == "tool_recommend":
        return {
            "task": text or f"Unknown MCP tool request: {_normalized_tool_name(args.get('name') or '')}",
            "project_id": project,
            "top_n": 3,
        }
    return args


async def _unknown_tool_llm_recovery(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from app.dependencies import get_llm_gateway

    allowed_tools = ["ask_project", "project_work", "project_context", "project_verify", "project_capture", "tool_recommend"]
    prompt = json.dumps(
        {
            "task": "Recover an unknown MCP tool call by selecting the best expert entrypoint. Return only JSON.",
            "unknown_tool_name": name,
            "normalized_tool_name": _normalized_tool_name(name),
            "arguments": {key: args.get(key) for key in sorted(args.keys())},
            "allowed_tools": allowed_tools,
            "output_schema": {
                "tool": "one allowed_tools value",
                "confidence": "number 0..1",
                "intent": "short natural-language intent/question to pass into the selected expert tool",
                "reason": "one sentence",
            },
            "safety": "Prefer expert helper facades over low-level tools. Do not authorize mutations.",
        },
        ensure_ascii=False,
    )
    response = await get_llm_gateway().generate(
        prompt,
        system="You are a strict JSON classifier for unknown MCP tool recovery. Return only a JSON object.",
        task_type="intent_classification",
        mode="economy",
        max_tokens=220,
        temperature=0.0,
        timeout=20.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    parsed = _extract_json_object(response)
    tool = str(parsed.get("tool") or "")
    if tool not in set(allowed_tools):
        return {}
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    return {
        "tool": tool,
        "confidence": max(0.0, min(1.0, confidence)),
        "intent": str(parsed.get("intent") or "").strip(),
        "reason": str(parsed.get("reason") or "").strip(),
    }


async def _recover_unknown_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    normalized = _normalized_tool_name(name)
    if not normalized:
        return None

    available_names = [str(tool.get("name") or "") for tool in TOOLS if tool.get("name")]
    if normalized in available_names:
        return {"tool": normalized, "args": args, "reason": "Normalized unknown tool name to a known MCP tool."}

    pattern = _unknown_tool_recovery_pattern(name, args)
    learned = _learned_route_match(
        facade="unknown_tool_recovery",
        text=pattern,
        allowed_intent_types={"ask_project", "project_work", "project_context", "project_verify", "project_capture", "tool_recommend"},
    )
    if learned and str(learned.get("tool") or ""):
        metadata = learned.get("metadata") or {}
        recovered_text = str(metadata.get("intent") or "").strip()
        tool_name = str(learned.get("tool") or "").strip()
        return {
            "tool": tool_name,
            "args": _unknown_tool_call_args(tool_name, {**args, "name": name}, recovered_text=recovered_text),
            "reason": str(learned.get("reason") or "Recovered unknown tool name from learned expert routing patterns."),
        }

    close = difflib.get_close_matches(normalized, available_names, n=1, cutoff=0.82)
    if close:
        return {
            "tool": close[0],
            "args": args,
            "reason": "Recovered unknown tool name by close match to a known MCP tool.",
        }

    decision = await _unknown_tool_llm_recovery(name, args)
    tool_name = str(decision.get("tool") or "").strip()
    if not tool_name:
        return None
    try:
        get_route_pattern_store().record(
            facade="unknown_tool_recovery",
            pattern=pattern,
            intent_type=tool_name,
            tool=tool_name,
            mutating=False,
            confidence=float(decision.get("confidence") or 0.0),
            source="llm",
            metadata={
                "matched_example": normalized,
                "reason": decision.get("reason") or "Recovered unknown tool through expert routing.",
                "intent": decision.get("intent") or "",
            },
        )
    except Exception:
        pass
    return {
        "tool": tool_name,
        "args": _unknown_tool_call_args(tool_name, {**args, "name": name}, recovered_text=str(decision.get("intent") or "").strip()),
        "reason": str(decision.get("reason") or "Recovered unknown tool via expert routing."),
    }


def _unknown_tool_error_message(name: str, args: dict[str, Any]) -> str:
    normalized = _normalized_tool_name(name)
    available_names = [str(tool.get("name") or "") for tool in TOOLS if tool.get("name")]
    close = difflib.get_close_matches(normalized, available_names, n=3, cutoff=0.6)
    hints: list[str] = []
    if close:
        hints.append("closest_tools=" + ", ".join(close))
    hints.append("start with mailbox_state for the current public workflow packet")
    hints.append("use mailbox_submit or mailbox_get for the public mailroom protocol")
    hints.append("use ask_project/project_work only when the mailbox packet directs a facade fallback")
    return f"Unknown tool: {name}. " + "; ".join(hints) + "."


def _infer_tool_family(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    if not name:
        return "general"
    if name in {"normalize_mcp_intent", "list_tool_families", "tool_family_tools", "tool_recommend", "tool_explain", "tool_feedback"}:
        if name == "normalize_mcp_intent":
            return "intent_routing"
        return "tool_discovery"
    if name in {
        "operational_tray",
        "upsert_knowledge_tree_node",
        "list_artifacts",
        "list_open_tasks",
        "get_task_execution_context",
        "reconcile_completed_checkpoints",
        "review_completed_checkpoint_scope",
        "review_completed_checkpoint_scopes",
        "pull_task_context",
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


_PUBLIC_SURFACE_TOOLS = ("help", "state", "get", "submit")
_COMPATIBILITY_SURFACE_TOOLS = {"put", "mailbox_state", "mailbox_submit", "mailbox_get"}


def _tool_surface_role(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    if name in _PUBLIC_SURFACE_TOOLS:
        return "public_entrypoint"
    if name in _COMPATIBILITY_SURFACE_TOOLS:
        return "compatibility_legacy"
    return "specialized_fallback"


def _tool_surface_guidance(role: str) -> str:
    if role == "public_entrypoint":
        return "Recommended public MCP surface. Start here before legacy, specialized, or debug tools."
    if role == "compatibility_legacy":
        return "Compatibility/legacy surface. Prefer help/state/get/submit unless this is explicitly required."
    return "Specialized fallback/debug surface. Use only when help/state/get/submit or workflow state explicitly directs it."


def _annotate_tool_surface(tool: dict[str, Any]) -> dict[str, Any]:
    enriched = deepcopy(tool)
    name = str(enriched.get("name") or "").strip()
    role = _tool_surface_role(name)
    guidance = _tool_surface_guidance(role)
    description = str(enriched.get("description") or "").strip()
    prefix = {
        "public_entrypoint": "[Recommended public entrypoint]",
        "compatibility_legacy": "[Compatibility/legacy]",
        "specialized_fallback": "[Specialized fallback]",
    }[role]
    if description and not description.startswith(prefix):
        enriched["description"] = f"{prefix} {description}"
    elif not description:
        enriched["description"] = f"{prefix} {guidance}"
    annotations = deepcopy(enriched.get("annotations") or {}) if isinstance(enriched.get("annotations"), dict) else {}
    annotations.update(
        {
            "mnemoforge_surface_role": role,
            "mnemoforge_recommended_start": role == "public_entrypoint",
            "mnemoforge_guidance": guidance,
        }
    )
    enriched["annotations"] = annotations
    enriched["_mnemoforge"] = {
        "surface_role": role,
        "recommended_start": role == "public_entrypoint",
        "public_surface": list(_PUBLIC_SURFACE_TOOLS),
        "guidance": guidance,
    }
    return enriched


def _annotated_tool_catalog() -> list[dict[str, Any]]:
    return [_annotate_tool_surface(tool) for tool in _tool_catalog()]


_COMPACT_TOOL_NAMES = (
    "help",
    "state",
    "get",
    "submit",
    "mailbox_state",
    "mailbox_submit",
    "mailbox_get",
    "ask_project",
    "project_work",
    "project_rules",
    "project_context",
    "project_verify",
    "project_capture",
    "operational_tray",
    "list_tool_families",
    "tool_recommend",
    "tool_family_tools",
    "memory_search",
    "memory_store",
    "memory_health",
)


def _summarize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"required": [], "properties": []}
    required = [str(item) for item in (schema.get("required") or []) if str(item).strip()]
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    summarized_properties: list[dict[str, Any]] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            summarized_properties.append({"name": str(name), "type": "any"})
            continue
        summary = {
            "name": str(name),
            "type": str(spec.get("type") or "any"),
        }
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            summary["enum"] = [str(item) for item in enum[:12]]
        if "default" in spec:
            summary["default"] = spec.get("default")
        summarized_properties.append(summary)
    return {
        "required": required,
        "properties": summarized_properties,
    }


def _compact_tool_catalog(*, limit: int = 12, schema_mode: str = "summary") -> list[dict[str, Any]]:
    by_name = {str(tool.get("name")): tool for tool in _annotated_tool_catalog()}
    names: list[str] = []
    for name in _COMPACT_TOOL_NAMES:
        if name in by_name and name not in names:
            names.append(name)
    for name in ("get_task_execution_context", "record_task_checkpoint", "draft_task_checkpoint"):
        if name in by_name and name not in names and len(names) < limit:
            names.append(name)
    tools = [deepcopy(by_name[name]) for name in names[: max(1, limit)]]
    if str(schema_mode or "summary").strip().lower() in {"full", "raw", "debug"}:
        return tools
    for tool in tools:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
            tool["inputSchema"] = schema
        tool["inputSummary"] = _summarize_input_schema(schema)
    return tools


def _default_tool_catalog_mode() -> str:
    return _normalize_tool_catalog_mode(os.getenv("MCP_TOOL_CATALOG_DEFAULT", "compact")) or "compact"


def _tools_list_payload(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    mode = str(params.get("mode") or params.get("catalog_mode") or _default_tool_catalog_mode()).strip().lower()
    if mode in {"compact", "staged", "tray"}:
        limit = int(params.get("limit") or 12)
        schema_mode = str(params.get("schema_mode") or params.get("tool_schema_mode") or "summary").strip().lower()
        if schema_mode not in {"summary", "full", "raw", "debug"}:
            schema_mode = "summary"
        tools = _compact_tool_catalog(limit=limit, schema_mode=schema_mode)
        catalog_meta = {
            "catalog_mode": "compact",
            "schema_mode": "full" if schema_mode in {"full", "raw", "debug"} else "summary",
            "full_catalog_available": True,
            "full_catalog_request": {"method": "tools/list", "params": {"mode": "full"}},
            "full_schema_request": {"method": "tools/list", "params": {"mode": "compact", "schema_mode": "full"}},
            "recommended_first_tool": "help",
            "reason": "Compact mode starts with the simple help/state/get/submit surface; use help for protocol guidance, state for available public forms, get for reads, and submit for governed form submissions.",
            "total_tools_available": len(_tool_catalog()),
            "returned_tools": len(tools),
        }
        return {
            "tools": tools,
            "_mnemoforge": catalog_meta,
            "_supermemory": catalog_meta,
        }
    return {
        "tools": _annotated_tool_catalog(),
        "_mnemoforge": {
            "catalog_mode": "full",
            "recommended_public_surface": list(_PUBLIC_SURFACE_TOOLS),
            "recommended_first_tool": "help",
            "warning": "Full catalog includes legacy, specialized, and debug/fallback tools. Do not use it as the starting workflow; start with help/state/get/submit.",
            "compact_request": {"method": "tools/list", "params": {"mode": "compact"}},
        },
        "_supermemory": {
            "catalog_mode": "full",
            "recommended_public_surface": list(_PUBLIC_SURFACE_TOOLS),
            "recommended_first_tool": "help",
            "warning": "Full catalog includes legacy, specialized, and debug/fallback tools. Do not use it as the starting workflow; start with help/state/get/submit.",
            "compact_request": {"method": "tools/list", "params": {"mode": "compact"}},
        },
    }


def _normalize_tool_catalog_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"compact", "staged", "tray"}:
        return "compact"
    if mode in {"full", "debug", "compat", "compatibility"}:
        return "full"
    return ""


def _normalize_context_hygiene_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"small", "small_context", "small-context", "tight", "compact", "low_context", "low-context"}:
        return "small_context"
    if mode in {"full", "debug", "verbose", "raw"}:
        return "full"
    return ""


def _mnemoforge_params(params: dict[str, Any]) -> dict[str, Any]:
    mnemoforge = params.get("_mnemoforge") if isinstance(params.get("_mnemoforge"), dict) else {}
    if mnemoforge:
        return mnemoforge
    return params.get("_supermemory") if isinstance(params.get("_supermemory"), dict) else {}


def _extract_requested_tool_catalog_mode(params: dict[str, Any]) -> str:
    mnemoforge = _mnemoforge_params(params)
    tool_catalog = mnemoforge.get("tool_catalog") if isinstance(mnemoforge.get("tool_catalog"), dict) else {}
    candidates = [
        tool_catalog.get("preferred_mode"),
        tool_catalog.get("mode"),
        mnemoforge.get("tool_catalog_mode"),
        params.get("tool_catalog_mode"),
    ]
    capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    experimental = capabilities.get("experimental") if isinstance(capabilities.get("experimental"), dict) else {}
    capability_mnemoforge = (
        capabilities.get("mnemoforge")
        if isinstance(capabilities.get("mnemoforge"), dict)
        else capabilities.get("supermemory")
        if isinstance(capabilities.get("supermemory"), dict)
        else {}
    )
    experimental_mnemoforge = (
        experimental.get("mnemoforge")
        if isinstance(experimental.get("mnemoforge"), dict)
        else experimental.get("supermemory")
        if isinstance(experimental.get("supermemory"), dict)
        else {}
    )
    candidates.extend(
        [
            capability_mnemoforge.get("tool_catalog_mode"),
            experimental_mnemoforge.get("tool_catalog_mode"),
        ]
    )
    for candidate in candidates:
        mode = _normalize_tool_catalog_mode(candidate)
        if mode:
            return mode
    return ""


def _extract_requested_context_hygiene_mode(params: dict[str, Any]) -> str:
    mnemoforge = _mnemoforge_params(params)
    context = mnemoforge.get("context") if isinstance(mnemoforge.get("context"), dict) else {}
    candidates = [
        context.get("hygiene_mode"),
        context.get("mode"),
        mnemoforge.get("context_hygiene_mode"),
        mnemoforge.get("context_profile"),
        params.get("context_hygiene_mode"),
        params.get("context_profile"),
        params.get("response_mode"),
    ]
    capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    experimental = capabilities.get("experimental") if isinstance(capabilities.get("experimental"), dict) else {}
    capability_mnemoforge = (
        capabilities.get("mnemoforge")
        if isinstance(capabilities.get("mnemoforge"), dict)
        else capabilities.get("supermemory")
        if isinstance(capabilities.get("supermemory"), dict)
        else {}
    )
    experimental_mnemoforge = (
        experimental.get("mnemoforge")
        if isinstance(experimental.get("mnemoforge"), dict)
        else experimental.get("supermemory")
        if isinstance(experimental.get("supermemory"), dict)
        else {}
    )
    candidates.extend(
        [
            capability_mnemoforge.get("context_hygiene_mode"),
            capability_mnemoforge.get("context_profile"),
            experimental_mnemoforge.get("context_hygiene_mode"),
            experimental_mnemoforge.get("context_profile"),
        ]
    )
    for candidate in candidates:
        mode = _normalize_context_hygiene_mode(candidate)
        if mode:
            return mode
    return ""


def _int_from_nested(*values: Any) -> int:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def _casefold_nested(*values: Any) -> str:
    return " ".join(str(value or "").casefold() for value in values if value not in (None, ""))


def _infer_small_context_modes(params: dict[str, Any]) -> dict[str, Any]:
    mnemoforge = _mnemoforge_params(params)
    context = mnemoforge.get("context") if isinstance(mnemoforge.get("context"), dict) else {}
    model = params.get("model") if isinstance(params.get("model"), dict) else {}
    model_info = params.get("modelInfo") if isinstance(params.get("modelInfo"), dict) else {}
    capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    experimental = capabilities.get("experimental") if isinstance(capabilities.get("experimental"), dict) else {}
    capability_mnemoforge = (
        capabilities.get("mnemoforge")
        if isinstance(capabilities.get("mnemoforge"), dict)
        else capabilities.get("supermemory")
        if isinstance(capabilities.get("supermemory"), dict)
        else {}
    )
    experimental_mnemoforge = (
        experimental.get("mnemoforge")
        if isinstance(experimental.get("mnemoforge"), dict)
        else experimental.get("supermemory")
        if isinstance(experimental.get("supermemory"), dict)
        else {}
    )
    context_window = _int_from_nested(
        params.get("model_context_window"),
        params.get("context_window"),
        params.get("contextWindow"),
        context.get("model_context_window"),
        context.get("context_window"),
        model.get("context_window"),
        model.get("contextWindow"),
        model_info.get("context_window"),
        model_info.get("contextWindow"),
        capability_mnemoforge.get("model_context_window"),
        experimental_mnemoforge.get("model_context_window"),
    )
    profile_text = _casefold_nested(
        params.get("agent_profile"),
        params.get("model_tier"),
        params.get("model_name"),
        context.get("profile"),
        context.get("model_tier"),
        model.get("tier"),
        model.get("name"),
        model_info.get("tier"),
        model_info.get("name"),
        capability_mnemoforge.get("agent_profile"),
        experimental_mnemoforge.get("agent_profile"),
    )
    inferred = False
    reason = ""
    if context_window and context_window < 64000:
        inferred = True
        reason = f"model_context_window<{64000}"
    elif any(token in profile_text for token in ("small", "mini", "local", "slm", "tight", "low-context", "low_context")):
        inferred = True
        reason = "small_model_profile"
    if not inferred:
        return {"tool_catalog_mode": "", "context_hygiene_mode": "", "reason": "", "model_context_window": context_window}
    return {
        "tool_catalog_mode": "compact",
        "context_hygiene_mode": "small_context",
        "reason": reason,
        "model_context_window": context_window,
    }


def _extract_runtime_profile_id(params: dict[str, Any], inferred_modes: dict[str, Any] | None = None) -> str:
    mnemoforge = _mnemoforge_params(params)
    context = mnemoforge.get("context") if isinstance(mnemoforge.get("context"), dict) else {}
    capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    experimental = capabilities.get("experimental") if isinstance(capabilities.get("experimental"), dict) else {}
    capability_mnemoforge = (
        capabilities.get("mnemoforge")
        if isinstance(capabilities.get("mnemoforge"), dict)
        else capabilities.get("supermemory")
        if isinstance(capabilities.get("supermemory"), dict)
        else {}
    )
    experimental_mnemoforge = (
        experimental.get("mnemoforge")
        if isinstance(experimental.get("mnemoforge"), dict)
        else experimental.get("supermemory")
        if isinstance(experimental.get("supermemory"), dict)
        else {}
    )
    candidates = [
        mnemoforge.get("runtime_profile_id"),
        mnemoforge.get("runtime_profile"),
        context.get("runtime_profile_id"),
        params.get("runtime_profile_id"),
        params.get("agent_profile"),
        capability_mnemoforge.get("runtime_profile_id"),
        experimental_mnemoforge.get("runtime_profile_id"),
    ]
    allowed = {"strong_mcp_operator", "weak_mcp_operator", "unknown_cli", "diagnostic_operator"}
    for candidate in candidates:
        profile = str(candidate or "").strip()
        if profile in allowed:
            return profile
    if inferred_modes and inferred_modes.get("reason"):
        return "weak_mcp_operator"
    return "unknown_cli"


def _extract_model_name(params: dict[str, Any]) -> str:
    mnemoforge = _mnemoforge_params(params)
    model = params.get("model") if isinstance(params.get("model"), dict) else {}
    model_info = params.get("modelInfo") if isinstance(params.get("modelInfo"), dict) else {}
    candidates = [
        params.get("model_name"),
        model.get("name"),
        model_info.get("name"),
        mnemoforge.get("model_name"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return "unknown-model"


async def _get_session_identity_defaults(session_id: str | None) -> dict[str, str]:
    if not session_id:
        return {}
    try:
        from app.services.mcp_session_store import get_session_store

        ctx = await get_session_store().get_context(session_id)
    except Exception:
        return {}
    if not isinstance(ctx, dict):
        return {}
    return {
        "agent_fingerprint": str(ctx.get("agent_fingerprint") or "").strip(),
        "runtime_profile_id": str(ctx.get("runtime_profile_id") or "").strip(),
    }


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
    if family == "tool_discovery" and any(token in text for token in ("tool", "tools", "mcp")):
        score += 3
    return score


def _tool_lifecycle_annotations(tool_name: str) -> dict[str, Any]:
    try:
        stage = get_tool_stage(str(tool_name or "").strip())
    except Exception:
        stage = "stable"
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


def _checkpoint_draft_recommended_next_tool(data: dict[str, Any]) -> str:
    return checkpoint_draft_recommended_next_tool(data)


_SMALL_CONTEXT_SERVICE_KEYS = {
    "stage",
    "rationale",
    "feedback_expected",
    "follow_up",
    "token_budget",
    "token_overhead",
    "coverage",
}


def _small_context_compact_value(value: Any, *, key: str = "") -> Any:
    if key in _SMALL_CONTEXT_SERVICE_KEYS:
        return None
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for child_key, child_value in value.items():
            cleaned = _small_context_compact_value(child_value, key=str(child_key))
            if cleaned is not None:
                compact[str(child_key)] = cleaned
        return compact
    if isinstance(value, list):
        compact_items: list[Any] = []
        for item in value:
            cleaned = _small_context_compact_value(item)
            if cleaned is not None:
                compact_items.append(cleaned)
        return compact_items
    return value


def _small_context_rule_refs(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    refs = []
    for item in items:
        if not isinstance(item, dict):
            refs.append(item)
            continue
        refs.append(
            {
                key: item.get(key)
                for key in ("id", "title", "scope", "status", "topic_path", "reason")
                if item.get(key) not in (None, "", [])
            }
        )
    return refs


def _rule_packet_tokens(*parts: str) -> set[str]:
    tokens: set[str] = set()
    for part in parts:
        for token in re.findall(r"[\w]+", str(part or "").casefold(), flags=re.UNICODE):
            if len(token) >= 3 or (len(token) >= 2 and any(ord(char) > 127 for char in token)):
                tokens.add(token)
    return tokens


def _rule_packet_score(rule: dict[str, Any], *, query_tokens: set[str], required_bonus: float) -> float:
    hay = _rule_packet_tokens(
        str(rule.get("id") or ""),
        str(rule.get("title") or ""),
        str(rule.get("reason") or ""),
        str(rule.get("topic_path") or ""),
        str(rule.get("statement") or ""),
        " ".join(str(x) for x in (rule.get("tags") or [])),
    )
    if not query_tokens:
        base = 0.0
    else:
        base = len(query_tokens.intersection(hay)) / max(1.0, float(len(query_tokens)))
    if str(rule.get("_rule_source") or "") == "required":
        base += required_bonus
    return min(1.0, max(0.0, base))


def _build_operational_rule_packet(context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    threshold = float(args.get("relevance_threshold", 0.25) or 0.25)
    threshold = max(0.0, min(1.0, threshold))
    top_limit = max(1, min(10, int(args.get("top_rules_limit", 3) or 3)))
    available_limit = max(1, min(30, int(args.get("available_rules_limit", 12) or 12)))
    required_bonus = 0.2

    required = context.get("required_rules") if isinstance(context.get("required_rules"), list) else []
    recommended = context.get("recommended_rules") if isinstance(context.get("recommended_rules"), list) else []
    combined: list[dict[str, Any]] = []
    for item in required:
        if isinstance(item, dict):
            merged = dict(item)
            merged["_rule_source"] = "required"
            combined.append(merged)
    for item in recommended:
        if isinstance(item, dict):
            merged = dict(item)
            merged["_rule_source"] = "recommended"
            combined.append(merged)

    intent_tokens = _rule_packet_tokens(
        str(args.get("task") or ""),
        str(args.get("intent") or ""),
        str(args.get("state") or ""),
        " ".join(str(x) for x in (args.get("changed_files") or [])),
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    for rule in combined:
        score = _rule_packet_score(rule, query_tokens=intent_tokens, required_bonus=required_bonus)
        if score >= threshold:
            scored.append((score, rule))
    scored.sort(key=lambda row: row[0], reverse=True)
    if not scored and combined:
        scored = [(0.0, rule) for rule in combined]

    top_rows = scored[:top_limit]
    top_ids = {str(row[1].get("id") or row[1].get("title") or "") for row in top_rows}
    applied_rules: list[dict[str, Any]] = []
    for score, rule in top_rows:
        clean_rule = {k: v for k, v in rule.items() if not str(k).startswith("_")}
        clean_rule["relevance_score"] = round(score, 4)
        clean_rule["rule_source"] = str(rule.get("_rule_source") or "")
        applied_rules.append(clean_rule)

    available_rows = [(score, rule) for score, rule in scored if str(rule.get("id") or rule.get("title") or "") not in top_ids]
    available_rules: list[dict[str, Any]] = []
    for score, rule in available_rows[:available_limit]:
        available_rules.append(
            {
                "rule_id": str(rule.get("id") or rule.get("title") or ""),
                "title": str(rule.get("title") or rule.get("id") or "rule"),
                "rule_source": str(rule.get("_rule_source") or ""),
                "relevance_score": round(score, 4),
                "why_matched": str(rule.get("reason") or ""),
            }
        )

    selected_rule_id = str(args.get("rule_id") or "").strip()
    selected_rule = None
    if selected_rule_id:
        for _, rule in scored:
            rid = str(rule.get("id") or rule.get("title") or "").strip()
            if rid == selected_rule_id:
                selected_rule = {k: v for k, v in rule.items() if not str(k).startswith("_")}
                break

    packet: dict[str, Any] = {
        "detail": detail,
        "relevance_threshold": threshold,
        "applied_rules": applied_rules,
        "available_rules": available_rules,
        "available_count": len(available_rows),
        "pull_hint": "Call operational_tray action=inspect with rule_id to fetch one rule in full form.",
    }
    if detail == "full":
        packet["available_rules_full"] = [
            {
                **{k: v for k, v in rule.items() if not str(k).startswith("_")},
                "relevance_score": round(score, 4),
                "rule_source": str(rule.get("_rule_source") or ""),
            }
            for score, rule in available_rows[:available_limit]
        ]
    if selected_rule is not None:
        packet["selected_rule"] = selected_rule
    return packet


def _sanitize_tool_result_for_context(text: str, *, context_hygiene_mode: str = "") -> str:
    if _normalize_context_hygiene_mode(context_hygiene_mode) != "small_context":
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text
    compact = _small_context_compact_value(data)
    if isinstance(compact, dict):
        for key in ("required_rules", "recommended_rules"):
            if key in compact:
                compact[key] = _small_context_rule_refs(compact[key])
        service_refs = compact.setdefault("_mnemoforge_refs", {})
        if isinstance(service_refs, dict):
            service_refs.setdefault("omitted_service_fields", len(_SMALL_CONTEXT_SERVICE_KEYS))
            service_refs.setdefault("full_response", "Repeat the same call with context_hygiene_mode=full or response_mode=full.")
            service_refs.setdefault("mode", "small_context")
            compact.setdefault("_supermemory_refs", service_refs)
    return json.dumps(compact, indent=2, ensure_ascii=False)


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
    return build_tool_feedback_envelope(
        tool_name=tool_name,
        tool_stage=tool_stage,
        valence=valence,
        worked=worked,
        friction=friction,
        suggestion=suggestion,
        task_context=task_context,
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
        missing_fields=missing_fields,
        feedback_id=feedback_id,
        assessment=assessment,
        scope=scope,
        what_was_tested=what_was_tested,
        expected_behavior=expected_behavior,
        observed_behavior=observed_behavior,
        next_action=next_action,
        should_promote=should_promote,
        confidence=confidence,
    )


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
        return {"type": "object", "properties": {}}
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        # Some clients are strict about inputSchema being an object.
        # Fall back to a safe empty object schema.
        return {"type": "object", "properties": {}}
    # Ensure required/properties shape exists (some loaders assume these keys exist).
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    return deepcopy(schema)



def _tool_example_payload(tool_name: str, *, intent: str, project_id: str = "") -> dict[str, Any]:
    name = str(tool_name or "").strip()
    project_id = str(project_id or "").strip()
    if name == "pull_task_context":
        payload = {"task_id": "<task_id>", "detail": "compact"}
        if project_id:
            payload["project"] = project_id
        return payload
    if name == "reopen_task":
        payload = {"task_id": "<task_id>", "status": "active", "reason": intent[:120] or "reopen_task", "acted_by": "user", "source": "mcp"}
        if project_id:
            payload["project"] = project_id
        return payload
    if name == "list_open_tasks":
        payload = {"project": project_id or "mnemoforge", "limit": 50}
        return payload
    if name == "project_work":
        return {"project": project_id or "mnemoforge", "intent": intent[:240], "allow_mutation": False}
    if name == "report_task_checkpoint":
        return {
            "project": project_id or "mnemoforge",
            "task_id": "<task_id>",
            "stage": "planning",
            "summary": intent[:160] or "Record task progress",
            "next_step": "Resume from the latest checkpoint.",
            "reason": "normalize_mcp_intent",
            "acted_by": "user",
            "source": "mcp",
        }
    if name == "enrich_task_with_context":
        return {"project_id": project_id or "mnemoforge", "task": intent[:240], "max_components": 3}
    if name == "get_task_execution_context":
        return {
            "project": project_id or "mnemoforge",
            "task": intent[:240] or "Verify a server-side change",
            "state": "verification",
            "intent": intent[:120],
            "changed_files": [],
        }
    if name == "operational_tray":
        return {
            "project": project_id or "mnemoforge",
            "task": intent[:240] or "Inspect the current operation tray.",
            "state": "planning",
            "action": "inspect",
            "intent": intent[:120],
        }
    if name == "resolve_artifact":
        return {"artifact_key": "task:mnemoforge:<local_id>", "acted_by": "user", "action_source": "normalize_mcp_intent", "reason": intent[:120]}
    if name == "reopen_artifact":
        return {"artifact_key": "task:mnemoforge:<local_id>", "project": project_id or "mnemoforge", "status": "active", "reason": "normalize_mcp_intent", "acted_by": "user", "source": "mcp"}
    if name == "reconcile_completed_checkpoints":
        return {"project": project_id or "mnemoforge", "close": False, "close_policy": "strict", "limit": 100}
    if name == "review_completed_checkpoint_scope":
        return {
            "project": project_id or "mnemoforge",
            "task_id": "<task_id>",
            "checkpoint_change_id": "<checkpoint_change_id>",
            "next_step_scope": "follow_up_task",
            "reason": intent[:120] or "Review completed checkpoint next_step scope.",
        }
    if name == "review_completed_checkpoint_scopes":
        return {
            "project": project_id or "mnemoforge",
            "decisions": [
                {
                    "task_id": "<task_id>",
                    "checkpoint_change_id": "<checkpoint_change_id>",
                    "next_step_scope": "follow_up_task",
                    "reason": intent[:120] or "Batch review completed checkpoint next_step scope.",
                }
            ],
        }
    if name == "list_tool_families":
        return {}
    if name == "tool_recommend":
        return {"task": intent[:240], "project_id": project_id or "", "top_n": 3}
    return {}


def _looks_like_reactivation_intent(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(
        term in lowered
        for term in (
            "reopen task",
            "reactivate task",
            "restore task",
            "make task active",
            "make it active",
            "reopen",
            "reactivate",
        )
    )


def _looks_like_checkpoint_resume_intent(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(
        term in lowered
        for term in (
            "continue task",
            "continue this task",
            "resume task",
            "resume current task",
            "resume from checkpoint",
            "pick up this task",
            "pull task context",
            "restore task context",
        )
    )


def _normalize_mcp_intent_lexical(intent: str, *, project_id: str = "", top_n: int = 3) -> dict[str, Any]:
    text = str(intent or "").strip()
    clean_project = str(project_id or "").strip()
    top_n = max(1, min(5, int(top_n or 3)))
    lowered = text.casefold()
    task_id_match = re.search(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", text)
    extracted_task_id = task_id_match.group(0) if task_id_match else ""

    if _looks_like_reactivation_intent(lowered):
        resolved_tool = "reopen_task"
        resolved_family = "project_knowledge"
        confidence = 0.96
        rationale = "Explicit task reactivation maps to reopen_task."
    elif _looks_like_checkpoint_resume_intent(lowered):
        resolved_tool = "pull_task_context"
        resolved_family = "project_knowledge"
        confidence = 0.94
        rationale = "Task continuation maps to pull_task_context for read-only checkpoint replay before any mutation."
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
    elif any(term in lowered for term in ("reconcile", "stale tail", "completed checkpoint", "checkpoint tails")):
        resolved_tool = "reconcile_completed_checkpoints"
        resolved_family = "project_knowledge"
        confidence = 0.83
        rationale = "Completed-checkpoint lifecycle reconciliation intent maps to reconcile_completed_checkpoints."
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


async def _normalize_mcp_intent_llm(intent: str, *, project_id: str = "", top_n: int = 3) -> dict[str, Any]:
    from app.dependencies import get_llm_gateway

    allowed_tools = [
        "pull_task_context",
        "reopen_task",
        "list_open_tasks",
        "report_task_checkpoint",
        "enrich_task_with_context",
        "reconcile_completed_checkpoints",
        "resolve_artifact",
        "reopen_artifact",
        "tool_recommend",
    ]
    prompt = json.dumps(
        {
            "task": "Normalize an MCP intent to the best canonical tool. Return only JSON.",
            "intent": intent,
            "project_id": project_id,
            "allowed_tools": allowed_tools,
            "output_schema": {
                "tool": "one allowed_tools value",
                "confidence": "number 0..1",
                "reason": "one sentence",
            },
            "safety": "Only choose the routing tool; do not invent arguments beyond intent normalization.",
        },
        ensure_ascii=False,
    )
    response = await get_llm_gateway().generate(
        prompt,
        system="You are a strict JSON classifier for normalize_mcp_intent. Return only a JSON object.",
        task_type="intent_classification",
        mode="economy",
        max_tokens=200,
        temperature=0.0,
        timeout=20.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    parsed = _extract_json_object(response)
    tool = str(parsed.get("tool") or "")
    if tool not in set(allowed_tools):
        return {}
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    return {
        "tool": tool,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(parsed.get("reason") or "").strip(),
    }


async def _normalize_mcp_intent(intent: str, *, project_id: str = "", top_n: int = 3) -> dict[str, Any]:
    lexical = _normalize_mcp_intent_lexical(intent, project_id=project_id, top_n=top_n)
    allowed = {
        "pull_task_context",
        "reopen_task",
        "list_open_tasks",
        "report_task_checkpoint",
        "enrich_task_with_context",
        "reconcile_completed_checkpoints",
        "resolve_artifact",
        "reopen_artifact",
        "tool_recommend",
    }
    learned = _learned_route_match(
        facade="normalize_mcp_intent",
        text=intent,
        allowed_intent_types=allowed,
    )
    if learned and str(learned.get("tool") or "") in allowed:
        resolved_tool = str(learned.get("tool") or "")
        result = _normalize_mcp_intent_lexical(intent, project_id=project_id, top_n=top_n)
        result["resolved_tool"] = resolved_tool
        result["resolved_family"] = _infer_tool_family(resolved_tool)
        result["submit_to"] = resolved_tool
        result["confidence"] = float(learned.get("confidence") or result["confidence"])
        result["rationale"] = str(learned.get("reason") or result["rationale"])
        result["example_payload"] = _tool_example_payload(resolved_tool, intent=str(intent or "").strip(), project_id=str(project_id or "").strip())
        task_id = _extract_task_id_from_text(str(intent or ""))
        if task_id and result["example_payload"].get("task_id") == "<task_id>":
            result["example_payload"]["task_id"] = task_id
        required = list(_tool_input_schema(resolved_tool).get("required") or [])
        result["required_fields"] = required
        result["optional_fields"] = [name for name in (result["example_payload"].keys()) if name not in required]
        result["missing_fields"] = [field for field in required if field not in result["example_payload"] or result["example_payload"].get(field) in {"", None, "<task_id>"}]
        result["ready_to_execute"] = not result["missing_fields"] and resolved_tool != "tool_recommend"
        result["route_telemetry"] = {
            "backend_used": learned.get("backend_used") or "learned_semantic",
            "matched_pattern_id": learned.get("pattern_id") or "",
            "matched_pattern_score": learned.get("score"),
            "matched_by": learned.get("matched_by") or "",
        }
        return result
    try:
        decision = await _normalize_mcp_intent_llm(intent, project_id=project_id, top_n=top_n)
    except Exception:
        decision = {}
    if not decision:
        lexical["route_telemetry"] = {"backend_used": "lexical", "fallback_reason": "LLM returned no valid normalized tool."}
        return lexical
    resolved_tool = str(decision["tool"])
    result = _normalize_mcp_intent_lexical(intent, project_id=project_id, top_n=top_n)
    result["resolved_tool"] = resolved_tool
    result["resolved_family"] = _infer_tool_family(resolved_tool)
    result["submit_to"] = resolved_tool
    result["confidence"] = float(decision.get("confidence") or result["confidence"])
    result["rationale"] = str(decision.get("reason") or result["rationale"])
    result["example_payload"] = _tool_example_payload(resolved_tool, intent=str(intent or "").strip(), project_id=str(project_id or "").strip())
    task_id = _extract_task_id_from_text(str(intent or ""))
    if task_id and result["example_payload"].get("task_id") == "<task_id>":
        result["example_payload"]["task_id"] = task_id
    required = list(_tool_input_schema(resolved_tool).get("required") or [])
    result["required_fields"] = required
    result["optional_fields"] = [name for name in (result["example_payload"].keys()) if name not in required]
    result["missing_fields"] = [field for field in required if field not in result["example_payload"] or result["example_payload"].get(field) in {"", None, "<task_id>"}]
    result["ready_to_execute"] = not result["missing_fields"] and resolved_tool != "tool_recommend"
    pattern_id = _record_learned_route_pattern(
        facade="normalize_mcp_intent",
        text=str(intent or "").strip(),
        route={"intent_type": resolved_tool, "tool": resolved_tool, "mutating": False, "confidence": result["confidence"], "reason": result["rationale"]},
        decision={"confidence": result["confidence"], "matched_example": str(intent or "").strip()[:120], "reason": result["rationale"]},
    )
    result["route_telemetry"] = {"backend_used": "llm", "learned_pattern_id": pattern_id or ""}
    return result


def _route_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w]+", str(text or "").casefold(), flags=re.UNICODE)
        if len(token) >= 3
    }


def _route_catalog_scores(text: str, catalog: tuple[dict[str, Any], ...], args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    lowered = str(text or "").casefold()
    intent_tokens = _route_tokens(lowered)
    candidates: list[dict[str, Any]] = []
    for route in catalog:
        best_score = 0.0
        best_example = ""
        for example in route.get("examples", ()):
            example_text = str(example).casefold()
            example_tokens = _route_tokens(example_text)
            if not example_tokens:
                continue
            overlap = len(intent_tokens & example_tokens)
            union = len(intent_tokens | example_tokens) or 1
            score = overlap / union
            if example_text in lowered:
                score += 0.45
            elif all(token in intent_tokens for token in example_tokens):
                score += 0.25
            if score > best_score:
                best_score = score
                best_example = str(example)
        bonus_terms = route.get("bonus_terms") or ()
        if bonus_terms and any(str(term).casefold() in lowered for term in bonus_terms):
            best_score += 0.08
        if args and route.get("arg_bonus"):
            for arg_name in route["arg_bonus"]:
                if args.get(arg_name):
                    best_score += 0.04
        candidates.append(
            {
                "intent_type": route["intent_type"],
                "tool": route["tool"],
                "score": round(min(best_score, 1.0), 3),
                "matched_example": best_example,
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["intent_type"]))
    return candidates


def _selected_catalog_route(
    text: str,
    catalog: tuple[dict[str, Any], ...],
    args: dict[str, Any] | None = None,
    *,
    min_score: float = 0.22,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = _route_catalog_scores(text, catalog, args)
    if not candidates or float(candidates[0].get("score") or 0.0) < min_score:
        return None, candidates[:3]
    best = candidates[0]
    route = next(item for item in catalog if item["intent_type"] == best["intent_type"])
    return route, candidates[:3]


def _route_needs_llm_disambiguation(candidates: list[dict[str, Any]]) -> bool:
    if not candidates:
        return True
    top = float(candidates[0].get("score") or 0.0)
    second = float(candidates[1].get("score") or 0.0) if len(candidates) > 1 else 0.0
    return top < 0.34 or (top - second) < 0.08


def _catalog_route_by_intent(catalog: tuple[dict[str, Any], ...], intent_type: str) -> dict[str, Any] | None:
    clean = str(intent_type or "").strip()
    return next((item for item in catalog if item["intent_type"] == clean), None)


def _task_id_from_open_task_item(item: dict[str, Any]) -> str:
    task_id = str(item.get("task_id") or item.get("id") or "").strip()
    if task_id:
        return task_id
    artifact_key = str(item.get("artifact_key") or "").strip()
    if artifact_key.startswith("task:"):
        parts = artifact_key.split(":")
        if len(parts) >= 3:
            return parts[-1].strip()
    return ""


def _annotate_open_tasks_with_claims(data: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    claim_filter = str(args.get("claim_filter") or "available").strip().lower()
    if claim_filter not in {"available", "claimed", "all"}:
        claim_filter = "available"
    include_claims = bool(args.get("include_claims", True))
    if not include_claims and claim_filter == "all":
        return data

    items = data.get("items") or []
    if not isinstance(items, list):
        return data
    if not items:
        enriched = dict(data)
        enriched["claim_filter"] = claim_filter
        enriched["claim_summary"] = {"available": 0, "claimed": 0, "returned": 0, "hidden_claimed": 0}
        return enriched

    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    try:
        from app.services.task_lease_service import get_task_lease_store

        store = get_task_lease_store()
    except Exception as exc:
        enriched = dict(data)
        enriched["claim_filter"] = claim_filter
        enriched["claim_summary"] = {
            "available": len([item for item in items if isinstance(item, dict)]),
            "claimed": 0,
            "returned": len([item for item in items if isinstance(item, dict)]),
            "hidden_claimed": 0,
            "unavailable": True,
        }
        enriched.setdefault("warnings", [])
        if isinstance(enriched["warnings"], list):
            enriched["warnings"].append(f"Task claim annotations unavailable: {_format_tool_error_brief(exc)}")
        return enriched
    visible: list[dict[str, Any]] = []
    hidden_claimed_count = 0
    claimed_count = 0
    available_count = 0

    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        task_id = _task_id_from_open_task_item(item)
        lease = store.get_active_claim(project=project, task_id=task_id) if task_id else None
        if lease:
            claimed_count += 1
            item["claim_status"] = "claimed"
            item["claim_available"] = False
            item["task_claim"] = lease.model_dump(mode="json")
        else:
            available_count += 1
            if include_claims:
                item["claim_status"] = "available"
                item["claim_available"] = True
                item["task_claim"] = None

        if claim_filter == "available" and lease:
            hidden_claimed_count += 1
            continue
        if claim_filter == "claimed" and not lease:
            continue
        visible.append(item)

    enriched = dict(data)
    enriched["items"] = visible
    enriched["claim_filter"] = claim_filter
    enriched["claim_summary"] = {
        "available": available_count,
        "claimed": claimed_count,
        "returned": len(visible),
        "hidden_claimed": hidden_claimed_count,
    }
    if hidden_claimed_count:
        enriched["hidden_claimed_count"] = hidden_claimed_count
    return enriched


def _task_assignment_safety(item: dict[str, Any]) -> dict[str, Any]:
    tags = {str(tag).strip().casefold() for tag in (item.get("tags") or []) if str(tag).strip()}
    if item.get("claim_status") == "claimed":
        return {
            "state": "blocked",
            "assignable": False,
            "reason": "task_is_already_claimed",
            "requires_review": False,
        }
    if bool(item.get("task_statement_incomplete")):
        return {
            "state": "needs_review",
            "assignable": False,
            "reason": "task_statement_incomplete",
            "requires_review": True,
        }
    dependency_fields = ("depends_on", "blocked_by", "sequential_after", "related_task_ids")
    if any(item.get(field) for field in dependency_fields) or tags & {"dependent", "blocked", "sequential", "needs_dependency_review"}:
        return {
            "state": "needs_review",
            "assignable": False,
            "reason": "dependency_or_sequence_marker_present",
            "requires_review": True,
        }
    if item.get("parallel_safe") is True or item.get("assignment_safety") == "independent" or tags & {"parallel_safe", "independent", "multi_agent_safe"}:
        return {
            "state": "independent",
            "assignable": True,
            "reason": "explicit_independent_marker",
            "requires_review": False,
        }
    return {
        "state": "needs_review",
        "assignable": False,
        "reason": "no_explicit_independence_evidence",
        "requires_review": True,
    }


def _annotate_open_tasks_with_assignment_safety(data: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    assignment_filter = str(args.get("assignment_filter") or "all").strip().lower()
    if assignment_filter not in {"all", "independent", "needs_review"}:
        assignment_filter = "all"
    items = data.get("items") or []
    if not isinstance(items, list):
        return data

    visible: list[dict[str, Any]] = []
    summary = {"independent": 0, "needs_review": 0, "blocked": 0, "returned": 0, "hidden": 0}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        safety = _task_assignment_safety(item)
        item["assignment_safety"] = safety
        state = str(safety["state"])
        if state in summary:
            summary[state] += 1
        if assignment_filter == "independent" and state != "independent":
            summary["hidden"] += 1
            continue
        if assignment_filter == "needs_review" and state != "needs_review":
            summary["hidden"] += 1
            continue
        visible.append(item)

    summary["returned"] = len(visible)
    enriched = dict(data)
    enriched["items"] = visible
    enriched["assignment_filter"] = assignment_filter
    enriched["assignment_summary"] = summary
    if assignment_filter == "independent":
        enriched["assignment_policy"] = (
            "Only tasks with explicit independence evidence are returned for multi-agent assignment; "
            "unclaimed alone is not enough."
        )
    return enriched


def _project_rule_query_tokens(args: dict[str, Any]) -> set[str]:
    text = " ".join(str(args.get(key) or "") for key in ("query", "intent", "task", "project"))
    return {token for token in re.findall(r"[a-zA-Z0-9_+-]{3,}", text.casefold())}


def _project_context_rule_refs(args: dict[str, Any]) -> list[dict[str, Any]]:
    project = str(args.get("project") or "").strip()
    status = str(args.get("status") or "active").strip().lower()
    if not project or status not in {"active", "all"}:
        return []
    tokens = _project_rule_query_tokens(args)
    should_include_testing_rules = not tokens or bool(
        tokens
        & {
            "test",
            "tests",
            "testing",
            "pytest",
            "docker",
            "contour",
            "verification",
            "verify",
            "rules",
            "laws",
            "constraints",
        }
    )
    if not should_include_testing_rules:
        return []
    try:
        from app.services.task_execution_context_service import _project_testing_rule_refs

        refs = _project_testing_rule_refs(project)
    except Exception:
        return []
    return [
        {
            "id": ref.id,
            "title": ref.title,
            "status": ref.status,
            "scope": ref.scope,
            "project": project,
            "topic_path": ref.topic_path,
            "rationale": ref.rationale,
            "reason": ref.reason,
            "is_project_local": True,
            "source": "project_context",
        }
        for ref in refs
        if status == "all" or ref.status == status
    ]


def _compact_project_work_result(route: dict[str, Any], result: Any) -> Any:
    if route.get("tool") == "list_open_tasks" and isinstance(result, dict):
        items = result.get("items") or []
        compact_items: list[dict[str, Any]] = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            compact_item = {
                "artifact_key": item.get("artifact_key"),
                "title": item.get("title"),
                "status": item.get("status"),
                "task_id": item.get("task_id"),
                "linked_artifact_key": item.get("linked_artifact_key"),
            }
            if compact_item.get("task_id"):
                compact_item["next_detail_form"] = {
                    "tool": "mailbox_submit",
                    "form_id": "get_task_context",
                    "payload": {
                        "project": item.get("project") or result.get("project"),
                        "task_id": compact_item["task_id"],
                        "detail": "compact",
                    },
                }
            if isinstance(item.get("task_claim"), dict):
                compact_item["claim_status"] = item.get("claim_status")
                compact_item["claimed_by"] = (item.get("task_claim") or {}).get("owner_agent")
            compact_items.append(compact_item)
        return compact_items
    if route.get("tool") == "pull_task_context" and isinstance(result, dict):
        return {
            "task_id": result.get("task_id"),
            "status": result.get("status"),
            "latest_checkpoint": result.get("latest_checkpoint"),
            "next_safe_action": result.get("next_safe_action"),
            "execution_readiness": result.get("execution_readiness"),
            "recommended_first_tool": result.get("recommended_first_tool"),
        }
    if route.get("tool") == "get_task_execution_context" and isinstance(result, dict):
        return {
            "state": result.get("state"),
            "readiness": result.get("readiness"),
            "required_rules": result.get("required_rules"),
            "recommended_rules": result.get("recommended_rules"),
            "risk_controls": result.get("risk_controls"),
            "next_transitions": result.get("next_transitions"),
        }
    if route.get("tool") == "task_capture_review" and isinstance(result, dict):
        return {
            "found": result.get("found"),
            "candidates": [
                {
                    "artifact_id": item.get("artifact_id"),
                    "kind": item.get("kind"),
                    "content": item.get("content"),
                    "confidence": item.get("confidence"),
                }
                for item in (result.get("candidates") or [])[:5]
                if isinstance(item, dict)
            ],
        }
    if route.get("tool") == "start_task_session" and isinstance(result, dict):
        return {
            "status": result.get("status"),
            "task_id": result.get("task_id"),
            "work_token": result.get("work_token"),
            "lease_id": (result.get("lease") or {}).get("lease_id"),
            "work_session_id": (result.get("work_session") or {}).get("work_id"),
            "auto_heartbeat": result.get("auto_heartbeat"),
        }
    return result


async def _resolve_mailbox_public_ref(api_base: str, args: dict[str, Any]) -> dict[str, Any] | None:
    return await build_simple_public_ref_response(
        api_base=api_base,
        args=args,
        dependencies=PublicRefDependencies(
            get=_get,
            get_task_context=_build_pull_task_context_payload,
            public_error_message=public_mailbox_error_message,
        ),
    )


def _simple_surface_dependencies() -> SimpleSurfaceDependencies:
    return SimpleSurfaceDependencies(
        get_session_identity_defaults=_get_session_identity_defaults,
        resolve_public_ref=_resolve_mailbox_public_ref,
        resolve_query=lambda base, query_args, sid: build_simple_get_query_response(
            api_base=base,
            args=query_args,
            session_id=sid,
            dependencies=SimpleReadDependencies(
                post=_post,
                query_project_expert=lambda expert_base, expert_args, expert_sid: _build_ask_project_payload(
                    expert_base,
                    expert_args,
                    session_id=expert_sid,
                ),
                extract_task_id_like=_extract_task_id_like_from_text,
            ),
        ),
        submit_mailbox_form=lambda submit_args, payload, base, sid: _build_mailbox_submit_packet(
            args=submit_args,
            payload=payload,
            api_base=base,
            session_id=sid,
        ),
        tool_surface_role=_tool_surface_role,
    )


async def _build_simple_help_payload(args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return build_simple_help_response(args)


async def _build_simple_state_payload(args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return await build_simple_state_response(
        args=args,
        session_id=session_id,
        dependencies=_simple_surface_dependencies(),
    )


async def _build_simple_get_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return await build_simple_get_response(
        api_base=api_base,
        args=args,
        session_id=session_id,
        dependencies=_simple_surface_dependencies(),
    )


async def _build_simple_submit_payload(
    api_base: str,
    args: dict[str, Any],
    *,
    session_id: str | None = None,
    public_tool_name: str = "submit",
) -> dict[str, Any]:
    return await build_simple_submit_response(
        api_base=api_base,
        args=args,
        session_id=session_id,
        public_tool_name=public_tool_name,
        dependencies=_simple_surface_dependencies(),
    )




def _project_rules_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return project_rules_route(
        args,
        select_catalog_route=lambda text, catalog, route_args: _selected_catalog_route(
            text,
            catalog,
            route_args,
            min_score=0.2,
        ),
        catalog_route_by_intent=_catalog_route_by_intent,
        backend_requested=_facade_backend_requested,
        llm_decision=llm_decision,
        scorer_meta=scorer_meta,
    )


async def _project_rules_route_with_backend(args: dict[str, Any]) -> dict[str, Any]:
    return await _facade_route_with_backend(
        facade="project_rules",
        args=args,
        catalog=_PROJECT_RULES_ROUTE_CATALOG,
        route_fn=_project_rules_route,
    )


async def _build_project_rules_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    route = await _project_rules_route_with_backend(args)
    allow_mutation = bool(args.get("allow_mutation", False))
    executed = False
    result: Any = None
    warnings: list[str] = list(route.get("warnings") or [])
    if route["mutating"] and not allow_mutation:
        warnings.append("Selected rule-governance route is mutating; set allow_mutation=true only after reviewing submit_payload.")
    else:
        result_text = await _execute_tool(route["tool"], route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    action_status = "executed" if executed else "needs_confirmation" if route["mutating"] else "ready"
    recommended_next_call = None
    do_not_call = []
    if action_status == "needs_confirmation":
        do_not_call = ["promote_rule_candidate", "revise_law_from_rule_candidate", "review_rule_candidate"]
        if route["tool"] in {"create_project_law", "create_rule_candidate"}:
            do_not_call = ["memory_store", *do_not_call]
    if not executed:
        recommended_next_call = {
            "tool": "project_rules" if route["mutating"] else route["tool"],
            "arguments": ({**args, "allow_mutation": True} if route["mutating"] else route["payload"]),
        }
    route_telemetry = _build_route_telemetry(
        facade="project_rules",
        route=route,
        executed=executed,
        warnings=warnings,
        args=args,
    )
    await _session_observe(session_id, "project_rules:route", {"route_telemetry": route_telemetry})
    return {
        "status": "executed" if executed else "planned",
        "facade": "project_rules",
        "project": args.get("project") or "mnemoforge",
        "intent": str(args.get("intent") or "").strip(),
        "action_status": action_status,
        "selected_route": _selected_route_public(route),
        "agent_action": {
            "one_sentence_summary": f"project_rules selected {route['tool']} for {route['intent_type']} with confidence {route['confidence']:.2f}.",
            "recommended_next_call": recommended_next_call,
            "confirmation_required": action_status == "needs_confirmation",
            "confirmation_phrase": "set allow_mutation=true after reviewing submit_payload" if action_status == "needs_confirmation" else "",
            "do_not_call": do_not_call,
            "warnings": warnings,
        },
        "executed": executed,
        "submit_payload": route["payload"],
        "result": result,
        "route_telemetry": route_telemetry,
        "warnings": warnings,
        "next_safe_action": "Continue from the executed rule route result." if executed else "Review submit_payload, then call agent_action.recommended_next_call exactly; do not call memory_store.",
    }


def _facade_action_card(
    *,
    facade: str,
    route: dict[str, Any],
    args: dict[str, Any],
    executed: bool,
    warnings: list[str],
    guarded_tools: list[str] | None = None,
) -> dict[str, Any]:
    action_status = "executed" if executed else "needs_confirmation" if route.get("mutating") else "ready"
    recommended_next_call = None
    if not executed:
        recommended_next_call = {
            "tool": facade if route.get("mutating") else route["tool"],
            "arguments": ({**args, "allow_mutation": True} if route.get("mutating") else route["payload"]),
        }
    return {
        "action_status": action_status,
        "one_sentence_summary": (
            f"{facade} selected {route['tool']} for {route['intent_type']} "
            f"with confidence {route['confidence']:.2f}."
        ),
        "recommended_next_call": recommended_next_call,
        "confirmation_required": action_status == "needs_confirmation",
        "confirmation_phrase": "set allow_mutation=true after reviewing submit_payload" if action_status == "needs_confirmation" else "",
        "do_not_call": guarded_tools if action_status == "needs_confirmation" else [],
        "why": route.get("reason"),
        "warnings": warnings,
    }


def _build_route_telemetry(
    *,
    facade: str,
    route: dict[str, Any],
    executed: bool,
    warnings: list[str],
    args: dict[str, Any],
) -> dict[str, Any]:
    scorer = route.get("scorer") if isinstance(route.get("scorer"), dict) else {}
    return {
        "facade": facade,
        "intent": str(args.get("intent") or "").strip()[:240],
        "intent_type": route.get("intent_type"),
        "underlying_tool": route.get("tool"),
        "mutating": bool(route.get("mutating")),
        "executed": bool(executed),
        "guardrail_triggered": bool(route.get("mutating")) and not bool(executed),
        "confidence": route.get("confidence"),
        "scorer_backend": scorer.get("backend_used") or scorer.get("backend_requested") or "rule_based",
        "fallback_used": bool(scorer.get("fallback_reason")) or route.get("tool") == "tool_recommend",
        "fallback_reason": scorer.get("fallback_reason") or "",
        "matched_pattern_id": scorer.get("matched_pattern_id") or scorer.get("learned_pattern_id") or "",
        "matched_pattern_score": scorer.get("matched_pattern_score"),
        "matched_by": scorer.get("matched_by") or "",
        "warnings": list(warnings),
        "reason": str(route.get("reason") or "").strip(),
        "project": route.get("payload", {}).get("project") or route.get("payload", {}).get("project_id") or args.get("project") or args.get("project_id") or "",
        "task_id": route.get("payload", {}).get("task_id") or args.get("task_id") or "",
    }


def _selected_route_public(route: dict[str, Any]) -> dict[str, Any]:
    selected = {
        "tool": route["tool"],
        "intent_type": route["intent_type"],
        "mutating": bool(route.get("mutating")),
        "confidence": route["confidence"],
        "reason": route["reason"],
    }
    if route.get("matched_example"):
        selected["matched_example"] = route.get("matched_example")
    if route.get("route_candidates"):
        selected["route_candidates"] = route.get("route_candidates")
    if isinstance(route.get("scorer"), dict):
        selected["scorer"] = route.get("scorer")
    return selected


def _wants_route_diagnostic(args: dict[str, Any]) -> bool:
    return bool(args.get("diagnostic")) or str(args.get("response_format") or "").strip().lower() == "diagnostic"


def _wants_route_answer(args: dict[str, Any]) -> bool:
    return bool(args.get("answer")) or str(args.get("response_format") or "").strip().lower() == "answer"


def _diagnostic_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:500]
    return str(value)


def _first_route_diagnostic_task_id(result: Any, *, preferred_task_id: str = "") -> str:
    if isinstance(result, dict):
        if result.get("task_id"):
            return str(result["task_id"])
        items = result.get("items")
        if isinstance(items, list) and items:
            preferred = str(preferred_task_id or "").strip().casefold()
            if preferred:
                for item in items:
                    if isinstance(item, dict) and str(item.get("task_id") or "").casefold().startswith(preferred):
                        return str(item["task_id"])
            first = items[0]
            if isinstance(first, dict) and first.get("task_id"):
                return str(first["task_id"])
    return ""


def _first_route_result_item(result: Any, *, preferred_task_id: str = "") -> dict[str, Any]:
    if isinstance(result, dict):
        if any(key in result for key in ("task_id", "title", "status", "artifact_key")):
            return result
        items = result.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            preferred = str(preferred_task_id or "").strip().casefold()
            if preferred:
                for item in items:
                    if isinstance(item, dict) and str(item.get("task_id") or "").casefold().startswith(preferred):
                        return item
            return items[0]
    return {}


def _format_route_diagnostic(data: dict[str, Any]) -> str:
    selected = data.get("selected_route") or {}
    scorer = selected.get("scorer") if isinstance(selected.get("scorer"), dict) else {}
    telemetry = data.get("route_telemetry") if isinstance(data.get("route_telemetry"), dict) else {}
    result = data.get("result")
    preferred_task_id = _extract_task_id_like_from_text(str(data.get("intent") or ""))
    executed = bool(data.get("executed"))
    guardrail_triggered = bool(selected.get("mutating")) and not executed
    lines = [
        "Mnemoforge route diagnostic",
        f"facade={_diagnostic_value(data.get('facade'))}",
        f"project={_diagnostic_value(data.get('project'))}",
        f"intent={_diagnostic_value(data.get('intent'))}",
        f"status={_diagnostic_value(data.get('status'))}",
        f"action_status={_diagnostic_value(data.get('action_status'))}",
        f"executed={_diagnostic_value(executed)}",
        f"guardrail_triggered={_diagnostic_value(guardrail_triggered)}",
        f"confirmation_required={_diagnostic_value(guardrail_triggered)}",
        f"route.tool={_diagnostic_value(selected.get('tool'))}",
        f"route.intent_type={_diagnostic_value(selected.get('intent_type'))}",
        f"route.mutating={_diagnostic_value(selected.get('mutating'))}",
        f"route.confidence={_diagnostic_value(selected.get('confidence'))}",
        f"scorer.backend_requested={_diagnostic_value(scorer.get('backend_requested'))}",
        f"scorer.backend_used={_diagnostic_value(scorer.get('backend_used'))}",
        f"scorer.llm_attempted={_diagnostic_value(scorer.get('llm_attempted'))}",
        f"scorer.fallback_reason={_diagnostic_value(scorer.get('fallback_reason'))}",
        f"scorer.learned_pattern_id={_diagnostic_value(scorer.get('learned_pattern_id'))}",
        f"scorer.matched_pattern_id={_diagnostic_value(scorer.get('matched_pattern_id'))}",
        f"telemetry.scorer_backend={_diagnostic_value(telemetry.get('scorer_backend'))}",
        f"telemetry.fallback_used={_diagnostic_value(telemetry.get('fallback_used'))}",
        f"telemetry.fallback_reason={_diagnostic_value(telemetry.get('fallback_reason'))}",
        f"telemetry.matched_pattern_id={_diagnostic_value(telemetry.get('matched_pattern_id'))}",
        f"telemetry.matched_pattern_score={_diagnostic_value(telemetry.get('matched_pattern_score'))}",
        f"telemetry.matched_by={_diagnostic_value(telemetry.get('matched_by'))}",
        f"warnings={_diagnostic_value(data.get('warnings') or telemetry.get('warnings') or [])}",
        f"first_task_id={_diagnostic_value(_first_route_diagnostic_task_id(result, preferred_task_id=preferred_task_id))}",
        f"next_safe_action={_diagnostic_value(data.get('next_safe_action'))}",
    ]
    return "\n".join(lines)


def _format_route_answer(data: dict[str, Any]) -> str:
    selected = data.get("selected_route") if isinstance(data.get("selected_route"), dict) else {}
    result = data.get("result")
    preferred_task_id = _extract_task_id_like_from_text(str(data.get("intent") or ""))
    first = _first_route_result_item(result, preferred_task_id=preferred_task_id)
    intent_type = str(selected.get("intent_type") or "")
    lines = ["Mnemoforge answer"]

    if selected.get("mutating") and not data.get("executed"):
        lines.append("Answer: No mutation was executed. Review the guarded route before allowing changes.")
        lines.append("executed=false")
        lines.append("mutation_executed=false")
        lines.append("confirmation_required=true")
        lines.append("do_not_claim_created=true")
    elif intent_type == "task_lookup":
        task_id = first.get("task_id") or _first_route_diagnostic_task_id(result, preferred_task_id=preferred_task_id)
        if task_id:
            lines.append(f"Answer: Found task {task_id}.")
        else:
            lines.append("Answer: No exact task was found in the first result.")
    elif data.get("facade") == "project_work" and intent_type == "next_priority":
        title = str(first.get("title") or "").strip()
        if title:
            lines.append(f"Answer: Next useful project action is {title}.")
        else:
            lines.append("Answer: No open project task was found.")
    elif intent_type == "project_readiness":
        lines.append("Answer: Project readiness route executed.")
    elif data.get("executed"):
        lines.append(f"Answer: {data.get('facade') or 'facade'} executed route {selected.get('tool') or ''}.")
    else:
        lines.append("Answer: Route was selected but not executed.")

    if isinstance(result, str) and result.strip() and not first:
        compact_result = re.sub(r"\s+", " ", result).strip()
        lines.append(f"result={compact_result[:900]}")
    if first.get("task_id"):
        lines.append(f"task_id={_diagnostic_value(first.get('task_id'))}")
    if first.get("title"):
        lines.append(f"title={_diagnostic_value(first.get('title'))}")
    if first.get("status"):
        lines.append(f"task_status={_diagnostic_value(first.get('status'))}")
    if first.get("artifact_key"):
        lines.append(f"artifact_key={_diagnostic_value(first.get('artifact_key'))}")
    if first.get("work_token"):
        lines.append(f"work_token={_diagnostic_value(first.get('work_token'))}")
    if first.get("lease_id"):
        lines.append(f"lease_id={_diagnostic_value(first.get('lease_id'))}")
    if first.get("work_session_id"):
        lines.append(f"work_session_id={_diagnostic_value(first.get('work_session_id'))}")
    if data.get("facade") == "project_work" and intent_type == "next_priority":
        lines.append(f"why={_diagnostic_value(selected.get('reason'))}")
    if data.get("warnings"):
        lines.append(f"warnings={_diagnostic_value(data.get('warnings'))}")
    if not data.get("executed") and data.get("next_safe_action"):
        lines.append(f"next_safe_action={_diagnostic_value(data.get('next_safe_action'))}")
    return "\n".join(lines)


def _ask_project_select_route_lexical(args: dict[str, Any]) -> dict[str, Any]:
    return select_ask_project_lexical_route(
        args,
        extract_task_id_like=_extract_task_id_like_from_text,
    )


async def _ask_project_llm_route(args: dict[str, Any]) -> dict[str, Any]:
    from app.dependencies import get_llm_gateway

    question = _ask_project_query_text(args)
    project = str(args.get("project") or args.get("project_id") or "mnemoforge").strip() or "mnemoforge"
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    response_format = _ask_project_response_format(args)
    prompt = json.dumps(
        {
            "task": "Choose the best ask_project expert facade. Return only JSON.",
            "question": question,
            "project": project,
            "allowed_facades": ["project_context", "project_work", "project_verify", "project_capture"],
            "output_schema": {
                "facade": "one allowed_facades value",
                "confidence": "number 0..1",
                "reason": "one sentence",
                "guardrail": "optional string; mention mutation caution when relevant",
            },
            "safety": "Prefer read-only routing. Mutation-like requests must remain guarded.",
        },
        ensure_ascii=False,
    )
    response = await get_llm_gateway().generate(
        prompt,
        system="You are a strict JSON classifier for ask_project routing. Return only a JSON object.",
        task_type="intent_classification",
        mode="economy",
        max_tokens=200,
        temperature=0.0,
        timeout=20.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    parsed = _extract_json_object(response)
    facade = str(parsed.get("facade") or "")
    if facade not in {"project_context", "project_work", "project_verify", "project_capture"}:
        return {}
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    route = {
        "facade": facade,
        "reason": str(parsed.get("reason") or "ask_project classified the question into an expert facade.").strip(),
        "confidence": max(0.0, min(1.0, confidence)),
        "response_format": response_format,
        "payload": {
            "project": project,
            "intent": question,
            "detail": detail,
            "response_format": response_format,
            "limit": int(args.get("limit") or 20),
            "allow_mutation": False,
        },
        "guardrail": str(parsed.get("guardrail") or "").strip(),
    }
    if facade == "project_context":
        route["payload"].pop("allow_mutation", None)
    return route


async def _ask_project_select_route(args: dict[str, Any]) -> dict[str, Any]:
    lexical_route = _ask_project_select_route_lexical(args)
    question = _ask_project_query_text(args)
    if not question:
        return lexical_route
    task_id_like = _extract_task_id_like_from_text(question)
    if task_id_like:
        lexical_route["scorer"] = {
            "backend_requested": "auto",
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        }
        lexical_route["structural_match"] = True
        invalidated = _invalidate_conflicting_learned_route(
            facade="ask_project",
            args={"question": question},
            structural_route={
                "tool": "project_context",
                "intent_type": "project_context",
            },
            allowed_intent_types={"project_context", "project_work", "project_verify", "project_capture"},
        )
        if invalidated:
            lexical_route["scorer"]["invalidated_learned_pattern_id"] = invalidated.get("pattern_id", "")
            lexical_route["scorer"]["invalidated_learned_pattern"] = invalidated
        return lexical_route
    learned = _learned_route_match(
        facade="ask_project",
        text=question,
        allowed_intent_types={"project_context", "project_work", "project_verify", "project_capture"},
    )
    if learned and str(learned.get("tool") or "") in {"project_context", "project_work", "project_verify", "project_capture"}:
        route = _ask_project_select_route_lexical({**args})
        route["facade"] = str(learned.get("tool"))
        route["reason"] = str(learned.get("reason") or route["reason"])
        route["confidence"] = float(learned.get("confidence") or route["confidence"])
        route["scorer"] = {
            "backend_requested": "auto",
            "backend_used": learned.get("backend_used") or "learned_semantic",
            "llm_attempted": False,
            "fallback_reason": "",
            "matched_pattern_id": learned.get("pattern_id") or "",
            "matched_pattern_score": learned.get("score"),
            "matched_by": learned.get("matched_by") or "",
        }
        if route["facade"] == "project_context":
            route["payload"].pop("allow_mutation", None)
        else:
            route["payload"]["allow_mutation"] = False
        return route
    try:
        llm_route = await _ask_project_llm_route(args)
    except Exception:
        llm_route = {}
    if not llm_route:
        route = lexical_route
        route["scorer"] = {
            "backend_requested": "auto",
            "backend_used": "lexical",
            "llm_attempted": True,
            "fallback_reason": "LLM returned no valid ask_project facade.",
        }
        return route
    pattern_id = _record_learned_route_pattern(
        facade="ask_project",
        text=question,
        route={"intent_type": llm_route["facade"], "tool": llm_route["facade"], "mutating": False, "confidence": llm_route["confidence"], "reason": llm_route["reason"]},
        decision={"confidence": llm_route["confidence"], "matched_example": question[:120], "reason": llm_route["reason"]},
    )
    llm_route["scorer"] = {
        "backend_requested": "auto",
        "backend_used": "llm",
        "llm_attempted": True,
        "fallback_reason": "",
        "learned_pattern_id": pattern_id or "",
    }
    return llm_route


def _format_ask_project_diagnostic(data: dict[str, Any]) -> str:
    route = data.get("selected_expert_route") if isinstance(data.get("selected_expert_route"), dict) else {}
    lines = [
        "Mnemoforge ask_project diagnostic",
        f"project={_diagnostic_value(data.get('project'))}",
        f"question={_diagnostic_value(data.get('question'))}",
        f"selected_facade={_diagnostic_value(route.get('facade'))}",
        f"response_format={_diagnostic_value(route.get('response_format'))}",
        f"confidence={_diagnostic_value(route.get('confidence'))}",
        f"reason={_diagnostic_value(route.get('reason'))}",
        f"guardrail={_diagnostic_value(route.get('guardrail'))}",
        f"underlying_text={_diagnostic_value(data.get('result_text'))}",
    ]
    return "\n".join(lines)


def _ask_project_evaluation_footer(args: dict[str, Any], result_text: str) -> str:
    footer = str(args.get("evaluation_footer") or "").strip().lower()
    if footer not in {"routine_reduction"}:
        return result_text
    ok = "yes" if str(result_text or "").strip() else "no"
    if "ROUTINE_REDUCTION_OK =" in result_text:
        return result_text
    return f"{result_text.rstrip()}\nROUTINE_REDUCTION_OK = {ok}"


async def _build_ask_project_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    route = await _ask_project_select_route(args)
    tool_name = str(route["facade"])
    payload = dict(route["payload"])
    result_text = await _execute_tool(tool_name, payload, api_base, session_id=session_id)
    result_text = _ask_project_evaluation_footer(args, result_text)
    return {
        "status": "executed",
        "facade": "ask_project",
        "project": payload.get("project") or payload.get("project_id") or "mnemoforge",
        "question": _ask_project_query_text(args),
        "selected_expert_route": {
            "facade": tool_name,
            "response_format": route["response_format"],
            "confidence": route["confidence"],
            "reason": route["reason"],
            "guardrail": route.get("guardrail") or "",
            "scorer": route.get("scorer") if isinstance(route.get("scorer"), dict) else None,
        },
        "result_text": result_text,
        "route_telemetry": {
            "facade": "ask_project",
            "underlying_facade": tool_name,
            "response_format": route["response_format"],
            "confidence": route["confidence"],
            "guardrail_triggered": bool(route.get("guardrail")),
            "mutating": False,
            "executed": True,
            "reason": route["reason"],
            "scorer_backend": ((route.get("scorer") or {}).get("backend_used") if isinstance(route.get("scorer"), dict) else "lexical") or "lexical",
            "matched_pattern_id": ((route.get("scorer") or {}).get("matched_pattern_id") if isinstance(route.get("scorer"), dict) else "") or ((route.get("scorer") or {}).get("learned_pattern_id") if isinstance(route.get("scorer"), dict) else ""),
        },
        "next_safe_action": "Use the answer directly, or ask for diagnostic details if the route looks wrong.",
    }


async def _run_facade_route(
    *,
    facade: str,
    route: dict[str, Any],
    args: dict[str, Any],
    api_base: str,
    session_id: str | None = None,
    guarded_tools: list[str] | None = None,
) -> dict[str, Any]:
    allow_mutation = bool(args.get("allow_mutation", False))
    executed = False
    result: Any = None
    warnings: list[str] = list(route.get("warnings") or [])
    semantic_rules = _build_semantic_rule_packet(facade=facade, route=route, args=args)
    if semantic_rules.get("blocked"):
        warnings.append("Semantic rule precondition failed; selected route execution was blocked.")
    if route.get("mutating") and not allow_mutation:
        warnings.append(f"Selected {facade} route is mutating; set allow_mutation=true only after reviewing submit_payload.")
    elif semantic_rules.get("blocked"):
        result = {
            "status": "conflict",
            "error": semantic_rules.get("block_error") or "rule_precondition_failed",
            "semantic_rules": semantic_rules,
            "next_safe_action": "Satisfy rule preconditions before execution.",
        }
        executed = True
    else:
        result_text = await _execute_tool(route["tool"], route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True

    action_card = _facade_action_card(
        facade=facade,
        route=route,
        args=args,
        executed=executed,
        warnings=warnings,
        guarded_tools=guarded_tools,
    )
    route_telemetry = _build_route_telemetry(
        facade=facade,
        route=route,
        executed=executed,
        warnings=warnings,
        args=args,
    )
    await _session_observe(session_id, f"{facade}:route", {"route_telemetry": route_telemetry})
    return {
        "status": "executed" if executed else "planned",
        "facade": facade,
        "project": route["payload"].get("project") or route["payload"].get("project_id") or args.get("project") or args.get("project_id") or "mnemoforge",
        "intent": str(args.get("intent") or "").strip(),
        "action_status": action_card["action_status"],
        "selected_route": _selected_route_public(route),
        "agent_action": action_card,
        "executed": executed,
        "submit_payload": route["payload"],
        "result": result,
        "semantic_rules": semantic_rules,
        "route_telemetry": route_telemetry,
        "warnings": warnings,
        "next_safe_action": "Continue from the executed route result." if executed else "Review submit_payload before confirming mutation.",
    }


_PROJECT_CONTEXT_ROUTE_CATALOG: tuple[dict[str, Any], ...] = tuple(
    route.model_dump()
    for route in load_route_catalog_spec("project_context").routes
)


_PROJECT_VERIFY_ROUTE_CATALOG: tuple[dict[str, Any], ...] = tuple(
    route.model_dump()
    for route in load_route_catalog_spec("project_verify").routes
)


_PROJECT_CAPTURE_ROUTE_CATALOG: tuple[dict[str, Any], ...] = tuple(
    route.model_dump()
    for route in load_route_catalog_spec("project_capture").routes
)


def _facade_backend_requested(args: dict[str, Any]) -> str:
    backend_requested = str(args.get("scorer_backend") or "auto").strip().lower() or "auto"
    return backend_requested if backend_requested in {"lexical", "auto", "llm"} else "auto"


def _facade_text(args: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            str(args.get("intent") or "").strip(),
            str(args.get("question") or "").strip(),
            str(args.get("query") or "").strip(),
            str(args.get("task") or "").strip(),
            str(args.get("summary") or "").strip(),
            str(args.get("raw_notes") or "").strip(),
            str(args.get("state") or "").strip(),
        )
        if part
    )


def _extract_task_id_from_text(text: str) -> str:
    match = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        str(text or ""),
    )
    return match.group(0) if match else ""


def _is_full_uuid(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            str(value or "").strip(),
        )
    )


def _extract_task_id_like_from_text(text: str) -> str:
    full = _extract_task_id_from_text(text)
    if full:
        return full
    match = re.search(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{1,4}){0,4}\b", str(text or ""))
    return match.group(0) if match else ""


def _learned_route_match(
    *,
    facade: str,
    text: str,
    allowed_intent_types: set[str],
) -> dict[str, Any] | None:
    try:
        return get_route_pattern_store().match(
            facade=facade,
            pattern=text,
            allowed_intent_types=allowed_intent_types,
        )
    except Exception:
        return None


def _record_learned_route_pattern(
    *,
    facade: str,
    text: str,
    route: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    if not text.strip() or not route.get("intent_type") or not route.get("tool"):
        return ""
    try:
        return get_route_pattern_store().record(
            facade=facade,
            pattern=text,
            intent_type=str(route["intent_type"]),
            tool=str(route["tool"]),
            mutating=bool(route.get("mutating")),
            confidence=float(decision.get("confidence") or route.get("confidence") or 0.0),
            source="llm",
            metadata={
                "matched_example": decision.get("matched_example") or route.get("matched_example") or "",
                "reason": decision.get("reason") or route.get("reason") or "",
            },
        )
    except Exception:
        return ""


def _invalidate_conflicting_learned_route(
    *,
    facade: str,
    args: dict[str, Any],
    structural_route: dict[str, Any],
    allowed_intent_types: set[str],
) -> dict[str, Any] | None:
    text = _facade_text(args)
    if not text.strip():
        return None
    try:
        store = get_route_pattern_store()
        learned = store.match(facade=facade, pattern=text, allowed_intent_types=allowed_intent_types)
    except Exception:
        return None
    if not learned:
        return None
    learned_tool = str(learned.get("tool") or "").strip()
    learned_intent = str(learned.get("intent_type") or "").strip()
    expected_tool = str(structural_route.get("tool") or "").strip()
    expected_intent = str(structural_route.get("intent_type") or "").strip()
    if learned_tool == expected_tool and learned_intent == expected_intent:
        return None
    pattern_id = str(learned.get("pattern_id") or "").strip()
    disabled = False
    disable_pattern = getattr(store, "disable_pattern", None)
    if callable(disable_pattern):
        try:
            disabled = bool(
                disable_pattern(
                    pattern_id,
                    reason="conflicts_with_structural_route",
                    metadata={
                        "facade": facade,
                        "expected_tool": expected_tool,
                        "expected_intent_type": expected_intent,
                        "learned_tool": learned_tool,
                        "learned_intent_type": learned_intent,
                    },
                )
            )
        except Exception:
            disabled = False
    return {
        "pattern_id": pattern_id,
        "disabled": disabled,
        "learned_tool": learned_tool,
        "learned_intent_type": learned_intent,
        "expected_tool": expected_tool,
        "expected_intent_type": expected_intent,
    }


async def _facade_route_with_backend(
    *,
    facade: str,
    args: dict[str, Any],
    catalog: tuple[dict[str, Any], ...],
    route_fn,
) -> dict[str, Any]:
    backend_requested = _facade_backend_requested(args)
    lexical_route = route_fn(
        args,
        scorer_meta={
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
    )
    candidates = lexical_route.get("route_candidates") or []
    if lexical_route.get("structural_match"):
        invalidated = _invalidate_conflicting_learned_route(
            facade=facade,
            args=args,
            structural_route=lexical_route,
            allowed_intent_types={str(route["intent_type"]) for route in catalog},
        )
        if invalidated:
            scorer = lexical_route.get("scorer") if isinstance(lexical_route.get("scorer"), dict) else {}
            lexical_route["scorer"] = {
                **scorer,
                "invalidated_learned_pattern_id": invalidated.get("pattern_id", ""),
                "invalidated_learned_pattern": invalidated,
            }
        return lexical_route
    should_try_llm = backend_requested == "llm" or (
        backend_requested == "auto" and _route_needs_llm_disambiguation(candidates)
    )
    if not should_try_llm:
        return lexical_route

    text = _facade_text(args)
    if backend_requested == "auto":
        learned = _learned_route_match(
            facade=facade,
            text=text,
            allowed_intent_types={str(route["intent_type"]) for route in catalog},
        )
        if learned:
            return route_fn(
                args,
                llm_decision=learned,
                scorer_meta={
                    "backend_requested": backend_requested,
                    "backend_used": learned.get("backend_used") or "learned_semantic",
                    "llm_attempted": False,
                    "fallback_reason": "",
                    "matched_pattern_id": learned.get("pattern_id") or "",
                    "matched_pattern_score": learned.get("score"),
                    "matched_by": learned.get("matched_by") or "",
                },
            )
    try:
        decision = await _facade_llm_disambiguate(
            facade=facade,
            text=text,
            args=args,
            candidates=candidates,
            catalog=catalog,
        )
    except Exception as exc:
        lexical_route["scorer"] = {
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": True,
            "fallback_reason": _format_tool_error_brief(exc, default="llm disambiguation failed"),
        }
        return lexical_route

    if not decision:
        lexical_route["scorer"] = {
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": True,
            "fallback_reason": f"LLM returned no valid {facade} intent_type.",
        }
        return lexical_route

    route = route_fn(
        args,
        llm_decision=decision,
        scorer_meta={
            "backend_requested": backend_requested,
            "backend_used": "llm",
            "llm_attempted": True,
            "fallback_reason": "",
            "llm_reason": str(decision.get("reason") or "").strip(),
        },
    )
    pattern_id = _record_learned_route_pattern(facade=facade, text=text, route=route, decision=decision)
    if pattern_id:
        route.setdefault("scorer", {})["learned_pattern_id"] = pattern_id
    return route


def _project_context_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = str(args.get("intent") or args.get("task") or "").strip()
    text = intent.casefold()
    project = str(args.get("project_id") or args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args.get("task_id") or _extract_task_id_like_from_text(intent)).strip()
    task_id_is_full = _is_full_uuid(task_id)
    task = str(args.get("task") or intent or "Retrieve project context.").strip()
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    limit = max(1, min(50, int(args.get("limit") or args.get("max_items_per_layer") or 10)))

    route = {
        "tool": "enrich_task_with_context",
        "intent_type": "enrich_context",
        "mutating": False,
        "confidence": 0.72,
        "reason": "General project-context intent maps to enrich_task_with_context.",
        "matched_example": "",
        "route_candidates": [],
        "scorer": scorer_meta or {"backend_requested": _facade_backend_requested(args), "backend_used": "lexical", "llm_attempted": False, "fallback_reason": ""},
        "payload": {
            "project_id": project,
            "task": task,
            "detail": detail,
            "context_profile": args.get("context_profile") or "hot_path",
            "max_components": max(1, min(20, int(args.get("max_components") or 5))),
        },
    }

    route_args = {**args, "task_id": task_id} if task_id else args
    catalog_route, route_candidates = _selected_catalog_route(intent, _PROJECT_CONTEXT_ROUTE_CATALOG, route_args)
    if llm_decision and _catalog_route_by_intent(_PROJECT_CONTEXT_ROUTE_CATALOG, str(llm_decision.get("intent_type") or "")):
        chosen = _catalog_route_by_intent(_PROJECT_CONTEXT_ROUTE_CATALOG, str(llm_decision.get("intent_type") or ""))
        assert chosen is not None
        catalog_route = chosen
        route_candidates = [
            {
                "intent_type": chosen["intent_type"],
                "tool": chosen["tool"],
                "score": round(float(llm_decision.get("confidence") or 0.0), 3),
                "matched_example": str(llm_decision.get("matched_example") or "llm_disambiguation"),
                "scorer": "llm",
            },
            *[item for item in route_candidates if item.get("intent_type") != chosen["intent_type"]],
        ][:3]
    route["route_candidates"] = route_candidates
    if catalog_route and route_candidates:
        route.update(
            tool=catalog_route["tool"],
            intent_type=catalog_route["intent_type"],
            confidence=round(min(0.95, 0.55 + float(route_candidates[0].get("score") or 0.0) * 0.4), 2),
            mutating=bool(catalog_route.get("mutating")),
            reason=catalog_route["reason"],
            matched_example=route_candidates[0].get("matched_example", ""),
        )

    if task_id and not task_id_is_full:
        route.update(
            tool="list_artifacts",
            intent_type="task_lookup",
            structural_match=True,
            confidence=0.8,
            reason="A partial task-id-like token was provided; list task artifacts so the agent can resolve the exact task_id before replay.",
            warnings=[
                "Partial task_id detected; resolve the exact task_id from result.items before calling pull_task_context."
            ],
            payload={
                "project": project,
                "type": "task",
                "limit": min(max(10, int(args.get("limit") or 50)), 200),
            },
        )
    elif route["intent_type"] == "task_details" or (task_id and route["intent_type"] == "enrich_context"):
        route.update(
            tool="pull_task_context",
            intent_type="task_details",
            structural_match=True,
            confidence=max(0.88, float(route.get("confidence") or 0.0)),
            reason="A concrete task-id context request maps to pull_task_context so the agent receives the task replay bundle directly.",
            payload={
                "project": project,
                "task_id": task_id,
                "detail": detail,
                "agent_id": args.get("agent_id") or "codex",
                "include_replay_bundle": bool(args.get("include_replay_bundle", detail == "full")),
            },
        )
    elif route["intent_type"] == "rules_context" or any(term in text for term in ("law", "laws", "rule", "rules", "constraint", "constraints")):
        route.update(
            tool="project_rules",
            intent_type="rules_context",
            confidence=0.85,
            reason="Rule/constraint context belongs to the project_rules facade.",
            payload={
                "project": project,
                "intent": args.get("intent") or "check active project laws",
                "status": args.get("status") or "active",
                "limit": min(max(1, int(args.get("limit") or 100)), 200),
            },
        )
    elif route["intent_type"] == "reconstruction_bundle" or any(term in text for term in ("reconstruct", "reconstruction", "source loss", "lost source")):
        route.update(
            tool="get_project_reconstruction_bundle",
            intent_type="reconstruction_bundle",
            confidence=0.86,
            reason="Source-loss or recovery context maps to the reconstruction bundle.",
            payload={
                "project_id": project,
                "detail": detail,
                "max_items_per_layer": limit,
            },
        )
    elif route["intent_type"] == "project_readiness" or any(term in text for term in ("readiness", "ready", "bootstrap", "onboard")):
        route.update(
            tool="get_project_readiness",
            intent_type="project_readiness",
            confidence=0.82,
            reason="Readiness/bootstrap context maps to get_project_readiness.",
            payload={"project_id": project},
        )

    route["payload"] = {key: value for key, value in route["payload"].items() if value not in (None, "")}
    return route


async def _build_project_context_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    route = await _facade_route_with_backend(
        facade="project_context",
        args=args,
        catalog=_PROJECT_CONTEXT_ROUTE_CATALOG,
        route_fn=_project_context_route,
    )
    return await _run_facade_route(facade="project_context", route=route, args=args, api_base=api_base, session_id=session_id)


def _project_verify_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = str(args.get("intent") or args.get("task") or "").strip()
    text = intent.casefold()
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    changed_files = _string_list_arg(args.get("changed_files"))
    state = str(args.get("state") or "").strip()
    if state not in {"planning", "implementation", "verification", "live_validation", "documentation", "checkpointing", "handoff", "operator_review"}:
        state = "live_validation" if any(term in text for term in ("restart", "health", "live", "server")) else "verification"
    task = str(args.get("task") or intent or "Verify current project work.").strip()

    route = {
        "tool": "get_task_execution_context",
        "intent_type": "verification_context",
        "mutating": False,
        "confidence": 0.82,
        "reason": "Verification/test intent first needs state-scoped project rules, Docker contour hints, and risk controls.",
        "matched_example": "",
        "route_candidates": [],
        "scorer": scorer_meta or {"backend_requested": _facade_backend_requested(args), "backend_used": "lexical", "llm_attempted": False, "fallback_reason": ""},
        "payload": {
            "project": project,
            "task_id": str(args.get("task_id") or "").strip(),
            "task": task,
            "state": state,
            "intent": intent,
            "changed_files": changed_files,
            "include_rules": True,
            "include_tools": True,
            "prior_stage_recorded": bool(args.get("prior_stage_recorded")) if "prior_stage_recorded" in args else None,
            "stage_evidence": _string_list_arg(args.get("stage_evidence")),
        },
    }

    catalog_route, route_candidates = _selected_catalog_route(intent, _PROJECT_VERIFY_ROUTE_CATALOG, args)
    if llm_decision and _catalog_route_by_intent(_PROJECT_VERIFY_ROUTE_CATALOG, str(llm_decision.get("intent_type") or "")):
        chosen = _catalog_route_by_intent(_PROJECT_VERIFY_ROUTE_CATALOG, str(llm_decision.get("intent_type") or ""))
        assert chosen is not None
        catalog_route = chosen
        route_candidates = [
            {
                "intent_type": chosen["intent_type"],
                "tool": chosen["tool"],
                "score": round(float(llm_decision.get("confidence") or 0.0), 3),
                "matched_example": str(llm_decision.get("matched_example") or "llm_disambiguation"),
                "scorer": "llm",
            },
            *[item for item in route_candidates if item.get("intent_type") != chosen["intent_type"]],
        ][:3]
    route["route_candidates"] = route_candidates
    if catalog_route and route_candidates:
        route.update(
            tool=catalog_route["tool"],
            intent_type=catalog_route["intent_type"],
            confidence=round(min(0.95, 0.55 + float(route_candidates[0].get("score") or 0.0) * 0.4), 2),
            mutating=bool(catalog_route.get("mutating")),
            reason=catalog_route["reason"],
            matched_example=route_candidates[0].get("matched_example", ""),
        )

    if route["intent_type"] == "health_check" or (
        any(term in text for term in ("health", "healthcheck", "status server", "server status")) and not any(term in text for term in ("restart", "test", "pytest"))
    ):
        route.update(
            tool="memory_health",
            intent_type="health_check",
            confidence=0.8,
            reason="Pure server health intent maps to the read-only health endpoint.",
            payload={},
        )
    elif route["intent_type"] == "restart_validation_plan" or "restart" in text:
        route.update(
            intent_type="restart_validation_plan",
            confidence=0.88,
            reason="Restart/live validation maps to execution context; external restart remains outside MCP and must observe the 120-second post-restart window.",
        )

    route["payload"] = {key: value for key, value in route["payload"].items() if value not in (None, "", [])}
    return route


async def _build_project_verify_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    route = await _facade_route_with_backend(
        facade="project_verify",
        args=args,
        catalog=_PROJECT_VERIFY_ROUTE_CATALOG,
        route_fn=_project_verify_route,
    )
    data = await _run_facade_route(facade="project_verify", route=route, args=args, api_base=api_base, session_id=session_id)
    if route["intent_type"] in {"verification_context", "restart_validation_plan"}:
        data["project_verify_guidance"] = {
            "docker_test_contour": "Use scripts/run_pytest_docker.ps1 for pytest so tests run in mcp-e2e-test-runner against memory-server-test/qdrant-test.",
            "restart_window_seconds": 120,
            "live_boundary": "Do not treat Docker test-contour success as live-dev validation; restart memory-server-dev and wait 120 seconds before live MCP/API checks.",
        }
    return data


def _project_capture_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = str(args.get("intent") or args.get("summary") or args.get("raw_notes") or "").strip()
    text = intent.casefold()
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args.get("task_id") or "").strip()
    work_id = str(args.get("work_id") or "").strip()
    changed_files = _string_list_arg(args.get("changed_files"))
    verification = _string_list_arg(args.get("verification"))
    raw_notes = str(args.get("raw_notes") or args.get("summary") or intent or "Capture project work.").strip()

    route = {
        "tool": "clerk_draft_report",
        "intent_type": "draft_capture",
        "mutating": False,
        "confidence": 0.78,
        "reason": "Draft/capture intent maps to a reviewable clerk draft before governed memory mutation.",
        "matched_example": "",
        "route_candidates": [],
        "scorer": scorer_meta or {"backend_requested": _facade_backend_requested(args), "backend_used": "lexical", "llm_attempted": False, "fallback_reason": ""},
        "payload": {
            "project": project,
            "task_id": task_id,
            "work_id": work_id,
            "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
            "task_title": str(args.get("task_title") or intent or "Project capture").strip()[:160],
            "raw_notes": raw_notes,
            "stage": str(args.get("stage") or "implementation").strip(),
            "status": str(args.get("status") or "active").strip(),
            "changed_files": changed_files,
            "verification": verification,
            "use_llm": bool(args.get("use_llm", False)),
        },
    }

    catalog_route, route_candidates = _selected_catalog_route(intent, _PROJECT_CAPTURE_ROUTE_CATALOG, args)
    if llm_decision and _catalog_route_by_intent(_PROJECT_CAPTURE_ROUTE_CATALOG, str(llm_decision.get("intent_type") or "")):
        chosen = _catalog_route_by_intent(_PROJECT_CAPTURE_ROUTE_CATALOG, str(llm_decision.get("intent_type") or ""))
        assert chosen is not None
        catalog_route = chosen
        route_candidates = [
            {
                "intent_type": chosen["intent_type"],
                "tool": chosen["tool"],
                "score": round(float(llm_decision.get("confidence") or 0.0), 3),
                "matched_example": str(llm_decision.get("matched_example") or "llm_disambiguation"),
                "scorer": "llm",
            },
            *[item for item in route_candidates if item.get("intent_type") != chosen["intent_type"]],
        ][:3]
    route["route_candidates"] = route_candidates
    if catalog_route and route_candidates:
        route.update(
            tool=catalog_route["tool"],
            intent_type=catalog_route["intent_type"],
            confidence=round(min(0.95, 0.55 + float(route_candidates[0].get("score") or 0.0) * 0.4), 2),
            mutating=bool(catalog_route.get("mutating")),
            reason=catalog_route["reason"],
            matched_example=route_candidates[0].get("matched_example", ""),
        )
    if route["intent_type"] == "record_stenographer_span" and not args.get("span_type"):
        route.update(
            tool="clerk_draft_report",
            intent_type="draft_capture",
            mutating=False,
            confidence=0.72,
            reason="Span recording requires span_type; falling back to a reviewable clerk draft.",
        )

    if route["intent_type"] == "list_stenographer_spans" or any(term in text for term in ("list spans", "show spans", "stenographer spans", "transcript spans")):
        route.update(
            tool="list_stenographer_spans",
            intent_type="list_stenographer_spans",
            confidence=0.84,
            reason="Span inspection intent maps to list_stenographer_spans.",
            payload={
                "project": project,
                "task_id": task_id,
                "work_id": work_id,
                "limit": max(1, min(200, int(args.get("limit") or 50))),
            },
        )
    elif (route["intent_type"] == "record_stenographer_span" and args.get("span_type")) or (any(term in text for term in ("record span", "stenographer", "scribe span", "capture span")) and args.get("span_type")):
        route.update(
            tool="record_stenographer_span",
            intent_type="record_stenographer_span",
            mutating=True,
            confidence=0.86,
            reason="Recording a stenographer span mutates session capture state and is guarded.",
            payload={
                "project": project,
                "task_id": task_id,
                "work_id": work_id,
                "span_type": args.get("span_type"),
                "content": raw_notes,
                "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
                "source": args.get("source") or "project_capture",
            },
        )
    elif route["intent_type"] == "record_work_result" or any(term in text for term in ("save", "record result", "work result", "handoff", "close")) or (
        "checkpoint" in text and not any(term in text for term in ("draft", "preview"))
    ):
        route.update(
            tool="record_work_result",
            intent_type="record_work_result",
            mutating=True,
            confidence=0.88,
            reason="Persisting work result/checkpoint mutates governed project memory and is guarded.",
            payload={
                "project": project,
                "task_id": task_id,
                "artifact_key": str(args.get("artifact_key") or "").strip(),
                "summary": raw_notes,
                "changed_files": changed_files,
                "verification": verification,
                "stage": str(args.get("stage") or "in_progress").strip(),
                "checkpoint_mode": args.get("checkpoint_mode") or "standard",
                "next_step": args.get("next_step") or "",
                "next_step_scope": args.get("next_step_scope") or "unknown",
                "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
                "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
                "work_token": str(args.get("work_token") or "").strip(),
                "source": "project_capture",
                "danger_mode": bool(args.get("danger_mode", False)),
                "danger_confirmation": str(args.get("danger_confirmation") or ""),
            },
        )

    route["payload"] = {
        key: value
        for key, value in route["payload"].items()
        if value not in (None, "", []) or key in ("danger_mode", "danger_confirmation")
    }
    return route


async def _build_project_capture_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    route = await _facade_route_with_backend(
        facade="project_capture",
        args=args,
        catalog=_PROJECT_CAPTURE_ROUTE_CATALOG,
        route_fn=_project_capture_route,
    )
    return await _run_facade_route(
        facade="project_capture",
        route=route,
        args=args,
        api_base=api_base,
        session_id=session_id,
        guarded_tools=["record_work_result", "record_task_checkpoint", "record_stenographer_span"],
    )


def _project_work_action_card(
    *,
    route: dict[str, Any],
    executed: bool,
    result: Any,
    warnings: list[str],
    args: dict[str, Any],
) -> dict[str, Any]:
    if executed:
        action_status = "executed"
        recommended_next_call = None
    elif route.get("mutating"):
        action_status = "needs_confirmation"
        recommended_next_call = {
            "tool": "project_work",
            "arguments": {
                **{key: value for key, value in args.items() if key not in {"allow_mutation"}},
                "allow_mutation": True,
            },
        }
    elif route.get("tool") == "tool_recommend":
        action_status = "needs_clarification"
        recommended_next_call = {"tool": "tool_recommend", "arguments": route["payload"]}
    else:
        action_status = "ready"
        recommended_next_call = {"tool": route["tool"], "arguments": route["payload"]}

    one_sentence_summary = (
        f"project_work selected {route['tool']} for {route['intent_type']} "
        f"with confidence {route['confidence']:.2f}."
    )
    if executed:
        one_sentence_summary += " The safe route was executed."
    elif route.get("mutating"):
        one_sentence_summary += " The route is guarded and needs explicit mutation confirmation."

    do_not_call = []
    if route.get("tool") in {"record_work_result", "record_task_checkpoint"} and not executed:
        do_not_call = ["record_task_checkpoint", "record_work_result"]
    elif route.get("tool") == "project_rules":
        do_not_call = ["promote_rule_candidate", "revise_law_from_rule_candidate"]

    return {
        "action_status": action_status,
        "one_sentence_summary": one_sentence_summary,
        "recommended_next_call": recommended_next_call,
        "confirmation_required": action_status == "needs_confirmation",
        "confirmation_phrase": "set allow_mutation=true after reviewing submit_payload" if action_status == "needs_confirmation" else "",
        "do_not_call": do_not_call,
        "why": route.get("reason"),
        "compact_result": _compact_project_work_result(route, result),
        "warnings": warnings,
    }


def _weak_model_mutation_guardrail(route: dict[str, Any], executed: bool, action_card: dict[str, Any]) -> dict[str, Any] | None:
    if executed or not route.get("mutating"):
        return None
    return {
        "mutation_executed": False,
        "confirmation_required": True,
        "state_change": "not_executed",
        "do_not_claim_created": True,
        "plain_instruction": (
            "No task, checkpoint, rule, or artifact was created or changed. "
            "Say that only a guarded route plan was returned. Execute only after "
            "reviewing submit_payload and calling the recommended_next_call with allow_mutation=true."
        ),
        "recommended_next_call": action_card.get("recommended_next_call"),
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def _project_work_llm_disambiguate(text: str, args: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    from app.dependencies import get_llm_gateway

    allowed = [route["intent_type"] for route in _PROJECT_WORK_ROUTE_CATALOG]
    route_brief = [
        {
            "intent_type": route["intent_type"],
            "tool": route["tool"],
            "mutating": route["mutating"],
            "examples": list(route["examples"])[:4],
        }
        for route in _PROJECT_WORK_ROUTE_CATALOG
    ]
    prompt = json.dumps(
        {
            "task": "Choose the best project_work route for the user intent. Return only JSON.",
            "allowed_intent_types": allowed,
            "intent": text,
            "explicit_context": {
                "project": args.get("project"),
                "task_id_present": bool(args.get("task_id")),
                "artifact_key_present": bool(args.get("artifact_key")),
                "changed_files_present": bool(args.get("changed_files")),
                "summary_present": bool(args.get("summary") or args.get("raw_notes")),
            },
            "lexical_candidates": candidates,
            "route_catalog": route_brief,
            "output_schema": {
                "intent_type": "one allowed_intent_types value",
                "confidence": "number 0..1",
                "matched_example": "closest example or short rationale phrase",
                "reason": "one sentence",
            },
            "safety": "Only classify route intent. Do not authorize mutations.",
        },
        ensure_ascii=False,
    )
    response = await get_llm_gateway().generate(
        prompt,
        system="You are a strict JSON classifier for MCP route selection. Return only a JSON object.",
        task_type="intent_classification",
        mode="economy",
        max_tokens=240,
        temperature=0.0,
        timeout=20.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    parsed = _extract_json_object(response)
    if str(parsed.get("intent_type") or "") not in set(allowed):
        return {}
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    parsed["confidence"] = max(0.0, min(1.0, confidence))
    return parsed


async def _facade_llm_disambiguate(
    *,
    facade: str,
    text: str,
    args: dict[str, Any],
    candidates: list[dict[str, Any]],
    catalog: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    from app.dependencies import get_llm_gateway

    allowed = [route["intent_type"] for route in catalog]
    route_brief = [
        {
            "intent_type": route["intent_type"],
            "tool": route["tool"],
            "mutating": route.get("mutating", False),
            "examples": list(route.get("examples", ()))[:4],
        }
        for route in catalog
    ]
    prompt = json.dumps(
        {
            "task": f"Choose the best {facade} route for the user intent. Return only JSON.",
            "allowed_intent_types": allowed,
            "intent": text,
            "explicit_context": {
                "project": args.get("project") or args.get("project_id"),
                "task_id_present": bool(args.get("task_id")),
                "changed_files_present": bool(args.get("changed_files")),
                "verification_present": bool(args.get("verification")),
                "raw_notes_present": bool(args.get("raw_notes") or args.get("summary")),
            },
            "lexical_candidates": candidates,
            "route_catalog": route_brief,
            "output_schema": {
                "intent_type": "one allowed_intent_types value",
                "confidence": "number 0..1",
                "matched_example": "closest example or short rationale phrase",
                "reason": "one sentence",
            },
            "safety": "Only classify route intent. Do not authorize mutations.",
        },
        ensure_ascii=False,
    )
    response = await get_llm_gateway().generate(
        prompt,
        system="You are a strict JSON classifier for MCP facade route selection. Return only a JSON object.",
        task_type="intent_classification",
        mode="economy",
        max_tokens=240,
        temperature=0.0,
        timeout=20.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    parsed = _extract_json_object(response)
    if str(parsed.get("intent_type") or "") not in set(allowed):
        return {}
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    parsed["confidence"] = max(0.0, min(1.0, confidence))
    return parsed


async def _project_work_route_with_backend(args: dict[str, Any]) -> dict[str, Any]:
    backend_requested = str(args.get("scorer_backend") or "auto").strip().lower() or "auto"
    if backend_requested not in {"lexical", "auto", "llm"}:
        backend_requested = "auto"
    lexical_route = _project_work_route(
        args,
        scorer_meta={
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
    )
    candidates = lexical_route.get("route_candidates") or []
    should_try_llm = backend_requested == "llm" or (
        backend_requested == "auto" and _project_work_needs_llm_disambiguation(candidates)
    )
    if not should_try_llm:
        return lexical_route

    text = " ".join(
        part
        for part in (
            str(args.get("intent") or "").strip(),
            str(args.get("summary") or "").strip(),
            str(args.get("raw_notes") or "").strip(),
            str(args.get("state") or "").strip(),
        )
        if part
    )
    if backend_requested == "auto":
        learned = _learned_route_match(
            facade="project_work",
            text=text,
            allowed_intent_types={str(route["intent_type"]) for route in _PROJECT_WORK_ROUTE_CATALOG},
        )
        if learned:
            return _project_work_route(
                args,
                llm_decision=learned,
                scorer_meta={
                    "backend_requested": backend_requested,
                    "backend_used": learned.get("backend_used") or "learned_semantic",
                    "llm_attempted": False,
                    "fallback_reason": "",
                    "matched_pattern_id": learned.get("pattern_id") or "",
                    "matched_pattern_score": learned.get("score"),
                    "matched_by": learned.get("matched_by") or "",
                },
            )
    try:
        decision = await _project_work_llm_disambiguate(text, args, candidates)
    except Exception as exc:
        lexical_route["scorer"] = {
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": True,
            "fallback_reason": _format_tool_error_brief(exc, default="llm disambiguation failed"),
        }
        return lexical_route

    if not decision:
        lexical_route["scorer"] = {
            "backend_requested": backend_requested,
            "backend_used": "lexical",
            "llm_attempted": True,
            "fallback_reason": "LLM returned no valid project_work intent_type.",
        }
        return lexical_route

    route = _project_work_route(
        args,
        llm_decision=decision,
        scorer_meta={
            "backend_requested": backend_requested,
            "backend_used": "llm",
            "llm_attempted": True,
            "fallback_reason": "",
            "llm_reason": str(decision.get("reason") or "").strip(),
        },
    )
    pattern_id = _record_learned_route_pattern(facade="project_work", text=text, route=route, decision=decision)
    if pattern_id:
        route.setdefault("scorer", {})["learned_pattern_id"] = pattern_id
    return route


async def _build_project_work_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    allow_mutation = bool(args.get("allow_mutation", False))
    route = await _project_work_route_with_backend(args)
    warnings: list[str] = []
    executed = False
    result: Any = None
    semantic_rules = _build_semantic_rule_packet(facade="project_work", route=route, args=args)
    lease_guard: dict[str, Any] | None = None
    if (
        allow_mutation
        and not semantic_rules.get("blocked")
        and route["mutating"]
        and route["tool"] != "start_task_session"
        and str((route.get("payload") or {}).get("task_id") or "").strip()
    ):
        route_payload = route.get("payload") or {}
        route_task_id = str(route_payload.get("task_id") or "").strip()
        lease_guard = _task_mutation_requires_owned_claim(
            project=str(route_payload.get("project") or args.get("project") or "mnemoforge"),
            task_id=route_task_id,
            owner_agent=str(
                route_payload.get("owner_agent")
                or route_payload.get("agent_id")
                or args.get("owner_agent")
                or args.get("agent_id")
                or "codex"
            ),
            owner_session_id=str(route_payload.get("session_id") or args.get("session_id") or session_id or ""),
            tool_name=f"project_work:{route['tool']}",
            work_token=str(route_payload.get("work_token") or args.get("work_token") or ""),
            danger_mode=bool(args.get("danger_mode", False)),
            danger_confirmation=str(args.get("danger_confirmation") or ""),
        )

    if route["mutating"] and not allow_mutation:
        warnings.append(
            "Selected route is mutating; project_work returned a route plan. Set allow_mutation=true only after reviewing the payload and guardrails."
        )
    elif semantic_rules.get("blocked"):
        warnings.append("Semantic rule precondition failed; selected route execution was blocked.")
        result = {
            "status": "conflict",
            "error": semantic_rules.get("block_error") or "rule_precondition_failed",
            "semantic_rules": semantic_rules,
            "next_safe_action": "Satisfy semantic rule preconditions before mutation.",
        }
        executed = True
    elif lease_guard:
        result = lease_guard
        executed = True
        warnings.append("Mutation blocked by task lease ownership policy.")
    elif route["tool"] == "list_open_tasks":
        query = build_list_open_tasks_query(route["payload"])
        result = await _get(api_base, f"/artifacts?{query}")
        result = _annotate_open_tasks_with_claims(result, route["payload"])
        result = _annotate_open_tasks_with_assignment_safety(result, route["payload"])
        executed = True
    elif route["tool"] == "pull_task_context":
        result = await _build_pull_task_context_payload(api_base, route["payload"])
        executed = True
    elif route["tool"] == "start_task_session":
        result_text = await _execute_tool("start_task_session", route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    elif route["tool"] == "finish_task_session":
        result_text = await _execute_tool("finish_task_session", route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    elif route["tool"] == "get_task_execution_context":
        result = await _post(api_base, "/task-execution-context", build_task_execution_context_payload(route["payload"]))
        executed = True
    elif route["tool"] == "task_capture_review":
        task_id = str(route["payload"].get("task_id") or "").strip()
        if not task_id:
            warnings.append("Task capture review requires task_id; call pull_task_context or provide task_id before reviewing drafts.")
        else:
            project = quote(str(route["payload"].get("project") or "mnemoforge"), safe="")
            limit = max(1, min(100, int(route["payload"].get("limit") or 10)))
            result = await _get(
                api_base,
                f"/project/tasks/{quote(task_id, safe='')}/capture-candidates?project={project}&limit={limit}",
            )
            executed = True
    elif route["tool"] in {"approve_checkpoint_draft", "reject_checkpoint_draft"}:
        if not str(route["payload"].get("draft_id") or "").strip():
            warnings.append("Checkpoint draft approval/rejection requires draft_id.")
        else:
            result_text = await _execute_tool(route["tool"], route["payload"], api_base, session_id=session_id)
            try:
                result = json.loads(result_text)
            except Exception:
                result = result_text
            executed = True
    elif route["tool"] == "record_work_result":
        result_text = await _execute_tool("record_work_result", route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    elif route["tool"] == "project_rules":
        result_text = await _execute_tool("project_rules", route["payload"], api_base)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    else:
        warnings.append("No confident project-work route was found; use tool_recommend or clarify the intent.")

    next_safe_action = "Review the selected route and execute the submit_payload if it matches the operator intent."
    if executed:
        next_safe_action = "Continue from the executed route result."
    elif route["tool"] == "project_rules":
        next_safe_action = "Use the project_rules/governance tools listed in submit_payload.suggested_first_tools."
    action_card = _project_work_action_card(
        route=route,
        executed=executed,
        result=result,
        warnings=warnings,
        args=args,
    )
    weak_model_guardrail = _weak_model_mutation_guardrail(route, executed, action_card)
    route_telemetry = _build_route_telemetry(
        facade="project_work",
        route=route,
        executed=executed,
        warnings=warnings,
        args=args,
    )
    await _session_observe(session_id, "project_work:route", {"route_telemetry": route_telemetry})

    return {
        "status": "executed" if executed else "planned",
        "action_status": action_card["action_status"],
        "facade": "project_work",
        "project": route["payload"].get("project") or route["payload"].get("project_id") or args.get("project") or "mnemoforge",
        "intent": str(args.get("intent") or "").strip(),
        "agent_action": action_card,
        "selected_route": {
            "tool": route["tool"],
            "family": route["family"],
            "intent_type": route["intent_type"],
            "confidence": route["confidence"],
            "mutating": route["mutating"],
            "reason": route["reason"],
            "matched_example": route.get("matched_example", ""),
            "route_candidates": route.get("route_candidates", []),
            "scorer": route.get("scorer", {}),
        },
        "routing_evidence": route["evidence"],
        "executed": executed,
        "submit_payload": route["payload"],
        "result": result,
        "semantic_rules": semantic_rules,
        "route_telemetry": route_telemetry,
        "compact_result": action_card["compact_result"],
        "warnings": warnings,
        "next_safe_action": next_safe_action,
        "weak_model_guardrail": weak_model_guardrail,
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
        if family == "project_knowledge" and _looks_like_reactivation_intent(lowered):
            reopen_first = next((name for name in preferred if name == "reopen_task"), "")
            if reopen_first:
                preferred = [reopen_first] + [name for name in preferred if name != reopen_first]
        elif family == "project_knowledge" and _looks_like_checkpoint_resume_intent(lowered):
            pull_first = next((name for name in preferred if name == "pull_task_context"), "")
            if pull_first:
                preferred = [pull_first] + [name for name in preferred if name != pull_first]
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
    if project_id and any(term in task_text.casefold() for term in ("task", "improvement", "artifact", "context", "readiness", "project")):
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
    if _looks_like_reactivation_intent(lowered):
        canonical_surface.extend([
            {
                "tool": "reopen_task",
                "family": "project_knowledge",
                "why": "Use only when an existing task must be made active again.",
            },
            {
                "tool": "report_task_checkpoint",
                "family": "project_knowledge",
                "why": "Use to persist the current stage and make the task recoverable.",
            },
        ])
    elif _looks_like_checkpoint_resume_intent(lowered):
        canonical_surface.extend([
            {
                "tool": "pull_task_context",
                "family": "project_knowledge",
                "why": "Use first for read-only checkpoint replay before claiming or mutating task state.",
            },
            {
                "tool": "start_task_session",
                "family": "project_knowledge",
                "why": "Use after replay when the agent is ready to execute and needs an owned lease.",
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
                "tool": "project_work",
                "family": "project_knowledge",
                "why": "Use as the thematic first-contact facade for project work before choosing specialized lifecycle tools.",
            },
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

    if extra and isinstance(extra.get("route_telemetry"), dict):
        route_events = list(context.get("route_telemetry") or [])
        route_event = dict(extra["route_telemetry"])
        route_event.setdefault("ts", time.time())
        route_events.append(route_event)
        context["route_telemetry"] = route_events[-_MAX_SESSION_TOOLS:]

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
    tool_definition("help"),
    tool_definition("state"),
    tool_definition("get"),
    tool_definition("submit"),
    tool_definition("put"),
    {
        "name": "ask_project",
        "description": (
            "Human-facing read-only project expert facade. Ask a natural project question; "
            "Mnemoforge chooses the thematic facade, route, and response format."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["question"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "project_id": {"type": "string"},
                "question": {"type": "string", "description": "Natural user question such as 'what is task 382e7306?' or 'is this repo usable yet?'"},
                "query": {
                    "type": "string",
                    "description": "Compatibility alias for question used by some MCP clients.",
                },
                "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
                "client_profile": {"type": "string", "enum": ["default", "local", "small_context", "agent"], "default": "default"},
                "response_format": {"type": "string", "enum": ["auto", "answer", "diagnostic", "json"], "default": "auto"},
                "evaluation_footer": {
                    "type": "string",
                    "enum": ["none", "routine_reduction"],
                    "default": "none",
                    "description": (
                        "Optional self-contained test footer for weak local models; "
                        "routine_reduction appends ROUTINE_REDUCTION_OK."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
        },
    },
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
                "project": {"type": "string", "description": "Project this memory belongs to"},
                "project_id": {"type": "string", "description": "Compatibility alias for project"},
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
                "project": {"type": "string", "description": "Filter to memories attributed to this project"},
                "project_id": {"type": "string", "description": "Compatibility alias for project"},
                "context_project": {"type": "string", "description": "Boost memories tagged project:<this> without filtering"},
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
            "Use this when you hit a limitation or bug in MnemoForge or any project. "
            "Saved improvements are reviewed during future development sessions."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "description"],
            "properties": {
                "title": {"type": "string", "description": "Short title of the issue or improvement"},
                "description": {"type": "string", "description": "Full description with context, steps to reproduce, expected behavior"},
                "project": {"type": "string", "default": "mnemoforge", "description": "Which project this applies to"},
                "agent_id": {"type": "string", "default": "llm", "description": "Who is reporting"},
                "importance_score": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 10,
                    "default": 0.7,
                    "description": "Importance on 0..1 scale. Values >1 and <=10 are accepted as 1..10 shorthand and normalized.",
                },
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
        "name": "project_rule_candidates_from_stenography",
        "description": (
            "Project explicit stenographer rule marker spans into reviewable rule candidates. "
            "Use after task closeout; this does not activate laws."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
            },
        },
    },
    {
        "name": "project_rules",
        "description": (
            "Thematic rule-governance facade. Use for intents such as 'this is a rule', "
            "'why did you forget the rule?', 'check project laws', 'review rule candidates', "
            "'propose new law', or 'promote/revise a law'. It routes to laws, candidates, review packets, promotion, "
            "and revision tools while guarding all mutating governance actions unless allow_mutation=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["intent"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "intent": {"type": "string"},
                "candidate_id": {"type": "string"},
                "law_id": {"type": "string"},
                "source_task_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["active", "user_confirmed", "candidate", "needs_clarification", "trial", "revision_pending", "rejected", "suppressed", "all"],
                    "default": "active",
                },
                "action": {"type": "string", "enum": ["reject", "suppress", "needs_clarification", "reopen"]},
                "reason": {"type": "string"},
                "title": {"type": "string"},
                "statement": {"type": "string"},
                "rationale": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}, "default": []},
                "target_scope": {"type": "string", "enum": ["project", "family", "domain", "principle", "meta"]},
                "target_status": {"type": "string", "enum": ["trial", "candidate", "proposed", "user_confirmed", "active"], "default": "trial"},
                "review_due": {"type": "boolean", "default": False},
                "review_after_days": {"type": "integer", "minimum": 0, "maximum": 365, "default": 7},
                "trial_days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 30},
                "confirmed_by": {"type": "string"},
                "allow_mutation": {"type": "boolean", "default": False},
                "diagnostic": {"type": "boolean", "default": False, "description": "Return a compact plain-text route diagnostic block."},
                "answer": {"type": "boolean", "default": False, "description": "Return a compact final-answer-shaped plain-text block."},
                "response_format": {"type": "string", "enum": ["json", "diagnostic", "answer"], "default": "json"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "max_matches": {"type": "integer", "minimum": 0, "maximum": 20, "default": 5},
                "acted_by": {"type": "string", "default": "codex"},
            },
        },
    },
    {
        "name": "project_context",
        "description": (
            "Thematic project-context facade. Use for 'give context', 'what matters here', "
            "'project constraints', 'readiness/bootstrap', and source-loss reconstruction context. "
            "It routes to enrichment, laws, readiness, and reconstruction surfaces without requiring agents to choose from the full catalog."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["intent"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "project_id": {"type": "string"},
                "intent": {"type": "string"},
                "task": {"type": "string"},
                "task_id": {"type": "string", "description": "Optional full or partial task id for direct task lookup/replay."},
                "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
                "context_profile": {"type": "string", "enum": ["default", "handoff_compact", "hot_path"], "default": "hot_path"},
                "status": {"type": "string", "enum": ["active", "user_confirmed", "all"], "default": "active"},
                "diagnostic": {"type": "boolean", "default": False, "description": "Return a compact plain-text route diagnostic block for local/weak MCP clients."},
                "answer": {"type": "boolean", "default": False, "description": "Return a final-answer-shaped plain-text block for small local models."},
                "response_format": {"type": "string", "enum": ["json", "diagnostic", "answer"], "default": "json"},
                "scorer_backend": {
                    "type": "string",
                    "enum": ["lexical", "auto", "llm"],
                    "default": "auto",
                    "description": "Route scorer backend. auto keeps deterministic lexical strong matches and uses cheap LLM disambiguation when no explicit route is found; lexical forces deterministic-only routing.",
                },
                "max_components": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                "max_items_per_layer": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "include_replay_bundle": {"type": "boolean", "default": False},
                "agent_id": {"type": "string", "default": "codex"},
            },
        },
    },
    {
        "name": "project_verify",
        "description": (
            "Thematic verification facade. Use for tests, live validation, restarts, health checks, "
            "and validation planning. It surfaces the project Docker test contour, live/test boundary, "
            "and 120-second post-restart window instead of making agents hunt through verification tools."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["intent"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "intent": {"type": "string"},
                "task": {"type": "string"},
                "task_id": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["planning", "implementation", "verification", "live_validation", "documentation", "checkpointing", "handoff", "operator_review"],
                },
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "stage_evidence": {"type": "array", "items": {"type": "string"}, "default": []},
                "prior_stage_recorded": {"type": "boolean"},
                "diagnostic": {"type": "boolean", "default": False, "description": "Return a compact plain-text route diagnostic block for local/weak MCP clients."},
                "answer": {"type": "boolean", "default": False, "description": "Return a final-answer-shaped plain-text block for small local models."},
                "response_format": {"type": "string", "enum": ["json", "diagnostic", "answer"], "default": "json"},
                "scorer_backend": {
                    "type": "string",
                    "enum": ["lexical", "auto", "llm"],
                    "default": "auto",
                    "description": "Route scorer backend. auto keeps deterministic lexical strong matches and uses cheap LLM disambiguation when no explicit route is found; lexical forces deterministic-only routing.",
                },
            },
        },
    },
    {
        "name": "project_capture",
        "description": (
            "Thematic capture facade. Use for stenographer/clerk drafts, checkpoints, handoff, "
            "and 'save the work' intents. Review-only drafts run directly; governed memory writes "
            "and stenographer writes are guarded unless allow_mutation=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["intent"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "intent": {"type": "string"},
                "task_id": {"type": "string"},
                "artifact_key": {"type": "string"},
                "work_id": {"type": "string"},
                "task_title": {"type": "string"},
                "summary": {"type": "string"},
                "raw_notes": {"type": "string"},
                "stage": {"type": "string", "default": "implementation"},
                "status": {"type": "string", "default": "active"},
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "verification": {"type": "array", "items": {"type": "string"}, "default": []},
                "next_step": {"type": "string"},
                "next_step_scope": {"type": "string"},
                "span_type": {"type": "string"},
                "allow_mutation": {"type": "boolean", "default": False},
                "use_llm": {"type": "boolean", "default": False},
                "diagnostic": {"type": "boolean", "default": False, "description": "Return a compact plain-text route diagnostic block for local/weak MCP clients."},
                "answer": {"type": "boolean", "default": False, "description": "Return a final-answer-shaped plain-text block for small local models."},
                "response_format": {"type": "string", "enum": ["json", "diagnostic", "answer"], "default": "json"},
                "scorer_backend": {
                    "type": "string",
                    "enum": ["lexical", "auto", "llm"],
                    "default": "auto",
                    "description": "Route scorer backend. auto keeps deterministic lexical strong matches and uses cheap LLM disambiguation when no explicit route is found; lexical forces deterministic-only routing.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "agent_id": {"type": "string", "default": "codex"},
                "acted_by": {"type": "string", "default": "codex"},
                "work_token": {
                    "type": "string",
                    "default": "",
                    "description": "Work token from start_task_session for mutating operations. Required when allow_mutation=true and a task is claimed.",
                },
            },
        },
    },
    {
        "name": "list_rule_candidates",
        "description": "List reviewable rule candidates created from explicit rule markers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["candidate", "needs_clarification", "trial", "revision_pending", "rejected", "suppressed"],
                },
                "source_task_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
        },
    },
    {
        "name": "get_rule_candidate_review_packet",
        "description": (
            "Build a read-only grouped review packet for rule candidates, including deterministic overlap "
            "with active laws and other candidates. Use before promotion, rejection, or merge decisions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["candidate", "needs_clarification", "trial", "revision_pending", "rejected", "suppressed"],
                    "default": "candidate",
                },
                "source_task_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "max_matches": {"type": "integer", "minimum": 0, "maximum": 20, "default": 5},
            },
        },
    },
    {
        "name": "review_rule_candidate",
        "description": (
            "Apply a safe operator review action to a rule candidate without mutating the law layer. "
            "Supports reject, suppress, needs_clarification, and reopen."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["candidate_id", "action", "reason"],
            "properties": {
                "candidate_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["reject", "suppress", "needs_clarification", "reopen"],
                },
                "reason": {"type": "string"},
                "acted_by": {"type": "string", "default": "codex"},
                "source": {"type": "string", "default": "mcp_rule_candidate_review"},
            },
        },
    },
    {
        "name": "promote_rule_candidate",
        "description": (
            "Promote a reviewed rule candidate into the law layer, preserving candidate evidence and "
            "recording promoted_law_id on the candidate. Defaults to proposed status; active/user_confirmed require confirmation metadata."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["candidate_id", "reason"],
            "properties": {
                "candidate_id": {"type": "string"},
                "title": {"type": "string"},
                "target_scope": {"type": "string", "enum": ["project", "family", "domain", "principle", "meta"]},
                "status": {"type": "string", "enum": ["proposed", "user_confirmed", "active"], "default": "proposed"},
                "reason": {"type": "string"},
                "acted_by": {"type": "string", "default": "codex"},
                "source": {"type": "string", "default": "mcp_rule_candidate_promotion"},
                "confirmed_by": {"type": "string"},
                "confirmation_source": {"type": "string", "default": "mcp_rule_candidate_promotion"},
            },
        },
    },
    {
        "name": "revise_law_from_rule_candidate",
        "description": (
            "Create a pending candidate_revision on an existing law from a reviewed rule candidate. "
            "The active law remains effective until the revision is explicitly confirmed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["candidate_id", "law_id", "reason"],
            "properties": {
                "candidate_id": {"type": "string"},
                "law_id": {"type": "string"},
                "reason": {"type": "string"},
                "acted_by": {"type": "string", "default": "codex"},
                "source": {"type": "string", "default": "mcp_rule_candidate_law_revision"},
                "title": {"type": "string"},
                "statement": {"type": "string"},
                "rationale": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
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
                "project": {"type": "string", "default": "mnemoforge", "description": "Project name to report on"},
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
    tool_definition("mailbox_state"),
    tool_definition("mailbox_submit"),
    tool_definition("mailbox_get"),
    tool_definition("list_open_tasks"),
    tool_definition("normalize_mcp_intent"),
    tool_definition("project_work"),
    tool_definition("project_workflow"),
    tool_definition("project_workflow_submit"),
    tool_definition("pull_task_context"),
    tool_definition("reopen_task"),
    tool_definition("list_tool_families"),
    tool_definition("tool_family_tools"),
    tool_definition("tool_explain"),
    tool_definition("tool_recommend"),
    tool_definition("tool_feedback"),
    tool_definition("get_work_session_state"),
    tool_definition("start_task_session"),
    tool_definition("finish_task_session"),
    tool_definition("start_work_session"),
    tool_definition("claim_task"),
    tool_definition("heartbeat_task_claim"),
    tool_definition("release_task_claim"),
    tool_definition("force_release_task_claim"),
    tool_definition("list_task_claims"),
    tool_definition("park_work_session"),
    tool_definition("resume_work_session"),
    tool_definition("end_work_session"),
    tool_definition("record_stenographer_span"),
    tool_definition("list_stenographer_spans"),
    tool_definition("clerk_draft_report"),
    tool_definition("draft_checkpoint_from_spans"),
    tool_definition("get_checkpoint_draft"),
    tool_definition("revise_checkpoint_draft"),
    tool_definition("approve_checkpoint_draft"),
    tool_definition("reject_checkpoint_draft"),
    tool_definition("draft_task_checkpoint"),
    tool_definition("record_work_result"),
    tool_definition("record_task_checkpoint"),
    tool_definition("report_task_checkpoint"),
    tool_definition("operational_tray"),
    tool_definition("upsert_knowledge_tree_node"),
    tool_definition("get_task_execution_context"),
    tool_definition("reconcile_completed_checkpoints"),
    tool_definition("review_completed_checkpoint_scope"),
    tool_definition("review_completed_checkpoint_scopes"),
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
            "Get a full overview of the MnemoForge system: what components exist, what each does, "
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
            "Package current task context in MnemoForge for pickup by another CLI tool. "
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
                "project_id": {"type": "string", "description": "Project identifier, e.g. 'mnemoforge'"},
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
    tool_definition("get_project_reconstruction_bundle"),
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
    "get_project_reconstruction_bundle",
    "plan_remote_snapshot",
    "sync_remote_snapshot",
    "get_storage_trust_status",
    "review_improvement",
    "list_project_aliases",
    "rename_project",
    "send_coordination_message",
    "pickup_coordination_messages",
    "list_coordination_messages",
    "update_coordination_message_status",
    "get_artifact",
    "list_artifacts",
    "mailbox_state",
    "mailbox_submit",
    "mailbox_get",
    "list_open_tasks",
    "operational_tray",
    "upsert_knowledge_tree_node",
    "get_task_execution_context",
    "reconcile_completed_checkpoints",
    "review_completed_checkpoint_scope",
    "review_completed_checkpoint_scopes",
    "normalize_mcp_intent",
    "project_workflow",
    "project_workflow_submit",
    "pull_task_context",
    "reopen_task",
    "list_tool_families",
    "tool_family_tools",
    "tool_explain",
    "tool_recommend",
    "tool_feedback",
    "record_task_checkpoint",
    "report_task_checkpoint",
    "draft_checkpoint_from_spans",
    "get_checkpoint_draft",
    "revise_checkpoint_draft",
    "approve_checkpoint_draft",
    "reject_checkpoint_draft",
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


def _checkpoint_handoff_payload(args: dict[str, Any], *, stage: str, status: str) -> dict[str, Any]:
    return checkpoint_handoff_payload(args, stage=stage, status=status)


def _checkpoint_scope_guard_decision(args: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    return checkpoint_scope_guard_decision(args, task)


async def _checkpoint_scope_guard(api_base: str, args: dict[str, Any]) -> dict[str, Any] | None:
    return await checkpoint_scope_guard(api_base, args, get=_get)


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
        "Next step scope": "next_step_scope",
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


def _checkpoint_stage_for_state(state: str) -> str:
    normalized = str(state or "").strip().lower()
    if normalized in {"planning", "checkpointing"}:
        return "planning"
    if normalized in {"implementation", "verification", "live_validation", "documentation", "operator_review"}:
        return "in_progress"
    if normalized == "handoff":
        return "handoff"
    return "in_progress"


def _operational_tray_target_tool(tray_action: str) -> str:
    return {
        "record_stage_evidence": "record_task_checkpoint",
        "record_checkpoint": "record_task_checkpoint",
        "draft_checkpoint": "clerk_draft_report",
        "review_rule_candidates": "get_rule_candidate_review_packet",
        "list_rule_candidates": "list_rule_candidates",
    }.get(str(tray_action or "").strip(), "")


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


def _project_pull_task_context_response(full_payload: dict[str, Any], *, detail: str, include_replay_bundle: bool, budget_args: dict[str, Any]) -> dict[str, Any]:
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


async def _build_pull_task_context_payload(api_base: str, args: dict[str, Any]) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
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

    # pull_task_context is read-only resume and must not start while the task is occupied.
    from app.services.task_lease_service import get_task_lease_store

    active_claim = get_task_lease_store().get_active_claim(project=project, task_id=task_id)
    if active_claim is not None:
        payload = {
            "project": project,
            "task_id": task_id,
            "status": "occupied",
            "occupied_by": {
                "owner_agent": active_claim.owner_agent,
                "owner_session_id": active_claim.session_id,
                "lease_id": active_claim.lease_id,
                "expires_at": active_claim.expires_at.isoformat(),
            },
            "next_safe_action": (
                "Task is occupied. Wait for lease release/expiry or coordinate handoff; "
                "do not start work from pull_task_context."
            ),
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
        "recommended_first_tool": "record_task_checkpoint" if not latest_checkpoint else "pull_task_context",
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
    return _project_pull_task_context_response(payload, detail=detail, include_replay_bundle=include_replay_bundle, budget_args=budget_args)


def _task_mutation_requires_owned_claim(
    *,
    project: str,
    task_id: str,
    owner_agent: str,
    owner_session_id: str,
    tool_name: str,
    work_token: str = "",
    danger_mode: bool = False,
    danger_confirmation: str = "",
) -> dict[str, Any] | None:
    return task_mutation_requires_owned_claim(
        project=project,
        task_id=task_id,
        owner_agent=owner_agent,
        owner_session_id=owner_session_id,
        tool_name=tool_name,
        work_token=work_token,
        danger_mode=danger_mode,
        danger_confirmation=danger_confirmation,
    )


def _semantic_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9_]+", " ", str(text or "").lower())
    return {token for token in cleaned.split() if len(token) >= 3}


def _semantic_rules_context_type(*, facade: str, route: dict[str, Any], args: dict[str, Any]) -> str:
    if facade == "project_verify":
        intent = str(route.get("intent_type") or "").strip()
        if intent in {"verification_context", "restart_validation_plan"}:
            return "post_implementation"
        return "post_validation"
    if facade == "project_work":
        if bool(route.get("mutating")):
            return "pre_implementation"
        if str(route.get("intent_type") or "") in {"pull_task_context", "task_lookup"}:
            return "task_framing"
        return "option_selection"
    return "task_enrichment"


def _build_semantic_rule_packet(
    *,
    facade: str,
    route: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    payload = route.get("payload") if isinstance(route.get("payload"), dict) else {}
    project = str(payload.get("project") or args.get("project") or args.get("project_id") or "mnemoforge").strip() or "mnemoforge"
    context_type = _semantic_rules_context_type(facade=facade, route=route, args=args)
    intent_text = " ".join(
        [
            str(args.get("intent") or ""),
            str(args.get("task") or ""),
            str(route.get("reason") or ""),
            str(route.get("intent_type") or ""),
            str(route.get("tool") or ""),
        ]
    ).strip()
    intent_tokens = _semantic_tokens(intent_text)

    try:
        candidates = get_active_operational_instincts(
            context_type=context_type,
            project_id=project,
            limit=25,
            record_activation=False,
        )
    except Exception as exc:
        return {
            "status": "fallback",
            "fallback_used": True,
            "reason": f"rules_unavailable:{_format_tool_error_brief(exc)}",
            "context_type": context_type,
            "project": project,
            "matched_rules": [],
            "applied_rule_count": 0,
            "blocked": False,
            "preconditions": [],
            "block_error": "",
        }

    scored: list[tuple[float, dict[str, Any]]] = []
    rank_bonus = {"P0": 2.0, "P1": 1.0, "P2": 0.4}
    now = time.time()
    for item in candidates:
        trigger = str(item.get("trigger") or "")
        action = str(item.get("action") or "")
        hay_tokens = _semantic_tokens(f"{trigger} {action} {' '.join(item.get('activation_tags') or [])}")
        overlap = len(intent_tokens.intersection(hay_tokens))
        score = float(overlap) + rank_bonus.get(str(item.get("rank") or ""), 0.0)
        if score <= 0:
            continue
        updated_at = float(item.get("updated_at") or 0.0)
        if updated_at > 0:
            age_days = max(0.0, (now - updated_at) / 86400.0)
            score += max(0.0, 0.5 - min(0.5, age_days / 30.0))
        scored.append((score, item))
    scored.sort(key=lambda row: row[0], reverse=True)
    top = [item for _, item in scored[:5]]

    command_text = " ".join(
        [
            str(args.get("command") or ""),
            str(args.get("test_command") or ""),
            str(args.get("cmd") or ""),
            " ".join(str(v) for v in (args.get("verification") or [])),
            str(args.get("intent") or ""),
        ]
    ).lower()
    wants_test = any(token in command_text for token in ("pytest", "run tests", "test "))
    docker_rule_active = any(
        ("docker" in str(item.get("action") or "").lower() and "contour" in str(item.get("action") or "").lower())
        or ("host pytest" in str(item.get("action") or "").lower())
        for item in top
    )
    if wants_test and not docker_rule_active and facade in {"project_verify", "project_work"}:
        docker_rule_active = True
    blocked = bool(
        wants_test
        and docker_rule_active
        and "pytest" in command_text
        and "run_pytest_docker" not in command_text
        and "docker" not in command_text
    )

    preconditions: list[dict[str, Any]] = []
    if wants_test and docker_rule_active:
        preconditions.append(
            {
                "id": "docker_test_contour",
                "required": True,
                "satisfied": not blocked,
                "message": "Run tests through the declared Docker test contour (for example scripts/run_pytest_docker.ps1).",
            }
        )

    return {
        "status": "applied" if top else "fallback",
        "fallback_used": not bool(top),
        "reason": "no_semantic_rule_match" if not top else "",
        "context_type": context_type,
        "project": project,
        "intent": intent_text[:240],
        "matched_rules": [
            {
                "instinct_id": str(item.get("instinct_id") or ""),
                "rank": str(item.get("rank") or ""),
                "action": str(item.get("action") or "")[:280],
                "activation_tags": list(item.get("activation_tags") or []),
            }
            for item in top
        ],
        "applied_rule_count": len(top),
        "blocked": blocked,
        "preconditions": preconditions,
        "block_error": "rule_precondition_failed:docker_test_contour" if blocked else "",
    }


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


def _string_list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _parse_artifact_key_ref(artifact_key: str) -> dict[str, str]:
    parts = str(artifact_key or "").split(":", 2)
    if len(parts) != 3:
        return {}
    artifact_type, project, local_id = [part.strip() for part in parts]
    if not artifact_type or not project or not local_id:
        return {}
    return {"type": artifact_type, "project": project, "local_id": local_id}


def _available_stenographer_spans(args: dict[str, Any], *, default_project: str, default_task_id: str = "") -> list[Any]:
    if not bool(args.get("use_clerk", False)) or bool(args.get("force_direct_checkpoint", False)):
        return []
    try:
        from app.services.stenographer_service import get_stenographer_store

        work_id = str(args.get("work_id") or "").strip()
        session_id_arg = str(args.get("session_id") or "").strip()
        task_id = str(args.get("task_id") or default_task_id or "").strip()
        return get_stenographer_store().list_spans(
            project=str(args.get("project") or default_project or "mnemoforge").strip() or None,
            task_id=task_id or None,
            work_id=work_id or None,
            agent_id=str(args.get("agent_id") or args.get("acted_by") or "codex").strip() or None if not work_id else None,
            session_id=session_id_arg or None,
            limit=int(args.get("clerk_span_limit") or args.get("limit") or 50),
        )
    except Exception:
        return []


async def _resolve_work_result_target(api_base: str, args: dict[str, Any]) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    artifact_key = str(args.get("artifact_key") or "").strip()
    task_id = str(args.get("task_id") or "").strip()
    linked_artifact_key = ""
    target_source = "provided"

    parsed = _parse_artifact_key_ref(artifact_key)
    if parsed:
        project = parsed["project"]
        if parsed["type"] == "task" and not task_id:
            task_id = parsed["local_id"]
        elif parsed["type"] == "improvement":
            linked_artifact_key = artifact_key
            try:
                artifact = await _get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
                linked = str(artifact.get("linked_artifact_key") or "").strip()
                linked_parsed = _parse_artifact_key_ref(linked)
                if linked_parsed.get("type") == "task":
                    task_id = linked_parsed["local_id"]
                    artifact_key = linked
            except Exception:
                pass
    elif task_id:
        artifact_key = f"task:{project}:{task_id}"

    if not task_id and not bool(args.get("skip_auto_task_match", False)):
        try:
            query = build_list_open_tasks_query({"project": project, "artifact_type": "task", "limit": 1})
            data = await _get(api_base, f"/artifacts?{query}")
            items = data.get("items") or []
            if items:
                item = items[0]
                item_key = str(item.get("artifact_key") or "").strip()
                item_parsed = _parse_artifact_key_ref(item_key)
                if item_parsed.get("type") == "task":
                    task_id = item_parsed["local_id"]
                    artifact_key = item_key
                    linked_artifact_key = str(item.get("linked_artifact_key") or "").strip()
                    target_source = "newest_open_task"
        except Exception:
            target_source = "unmatched"

    return {
        "project": project,
        "task_id": task_id,
        "artifact_key": artifact_key,
        "linked_artifact_key": linked_artifact_key,
        "target_source": target_source if task_id else "unmatched",
    }


async def _build_mailbox_submit_packet(
    *,
    args: dict[str, Any],
    payload: dict[str, Any],
    api_base: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    return await build_mailbox_action_submit_packet(
        args=args,
        payload=payload,
        api_base=api_base,
        session_id=session_id,
        dependencies=MailboxActionDependencies(
            get=_get,
            post=_post,
            execute_tool=_execute_tool,
            get_session_identity_defaults=_get_session_identity_defaults,
            task_mutation_guard=_task_mutation_requires_owned_claim,
        ),
    )


async def _execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None) -> str:
    await _session_observe(session_id, name, args)
    try:
        observe_tool_use(name)
    except Exception:
        pass

    # Server-side observer: works for any MCP client, no client hooks needed.
    # Runs asynchronously and never blocks tool execution.
    asyncio.create_task(_mcp_live_observe(name, args, api_base))

    if name == "help":
        data = await _build_simple_help_payload(args, session_id=session_id)
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "state":
        data = await _build_simple_state_payload(args, session_id=session_id)
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "get":
        data = await _build_simple_get_payload(api_base, args, session_id=session_id)
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name in {"put", "submit"}:
        data = await _build_simple_submit_payload(api_base, args, session_id=session_id, public_tool_name=name)
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "ask_project":
        data = await _build_ask_project_payload(api_base, args, session_id=session_id)
        if str(args.get("response_format") or "").strip().lower() == "diagnostic":
            return _format_ask_project_diagnostic(data)
        if str(args.get("response_format") or "").strip().lower() == "json":
            return json.dumps(data, indent=2, ensure_ascii=False)
        return str(data.get("result_text") or "")

    grouped_result = await execute_grouped_memory_or_runtime_action(
        name=name,
        args=args,
        api_base=api_base,
        dependencies=GroupedToolDispatchDependencies(get=_get, post=_post, delete=_delete),
    )
    if grouped_result is not None:
        return grouped_result

    if name == "report_issue":
        data = await _post(api_base, "/improvements", args)
        return f"Improvement reported: {data['id']}\nTitle: {data['title']}\nStatus: {data['status']}"

    elif name == "review_improvement":
        return await execute_artifact_lifecycle_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ArtifactLifecycleActionDependencies(get=_get, post=_post, patch=_patch),
        )

    elif name in {"list_project_aliases", "rename_project", "list_project_laws", "get_project_law", "create_project_law", "create_rule_candidate"}:
        return await execute_project_governance_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ProjectGovernanceActionDependencies(
                get=_get,
                post=_post,
                annotate_payload=_annotate_structured_tool_payload,
                project_context_rule_refs=_project_context_rule_refs,
            ),
        )

    elif name == "project_rule_candidates_from_stenography":
        payload = {
            "project": args.get("project") or "mnemoforge",
            "limit": int(args.get("limit") or 500),
        }
        data = await _post(api_base, "/laws/candidates/project-from-stenography", payload)
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "project_rules":
        data = await _build_project_rules_payload(api_base, args, session_id=session_id)
        if _wants_route_diagnostic(args):
            return _format_route_diagnostic(data)
        if _wants_route_answer(args):
            return _format_route_answer(data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "project_context":
        data = await _build_project_context_payload(api_base, args, session_id=session_id)
        if _wants_route_diagnostic(args):
            return _format_route_diagnostic(data)
        if _wants_route_answer(args):
            return _format_route_answer(data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "project_verify":
        data = await _build_project_verify_payload(api_base, args, session_id=session_id)
        if _wants_route_diagnostic(args):
            return _format_route_diagnostic(data)
        if _wants_route_answer(args):
            return _format_route_answer(data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "project_capture":
        data = await _build_project_capture_payload(api_base, args, session_id=session_id)
        if _wants_route_diagnostic(args):
            return _format_route_diagnostic(data)
        if _wants_route_answer(args):
            return _format_route_answer(data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name in {
        "list_rule_candidates",
        "get_rule_candidate_review_packet",
        "review_rule_candidate",
        "promote_rule_candidate",
        "revise_law_from_rule_candidate",
        "expire_trial_rule_candidates",
    }:
        return await execute_project_governance_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ProjectGovernanceActionDependencies(
                get=_get,
                post=_post,
                annotate_payload=_annotate_structured_tool_payload,
                project_context_rule_refs=_project_context_rule_refs,
            ),
        )

    elif name in {"list_learning_candidates", "approve_learning_candidate", "defer_learning_candidate", "reject_learning_candidate"}:
        return await execute_artifact_lifecycle_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ArtifactLifecycleActionDependencies(get=_get, post=_post, patch=_patch),
        )

    elif name == "improvements_report":
        project = args.get("project", "mnemoforge")
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

    elif name in {"set_canonical_status", "merge_canonicals"}:
        return await execute_artifact_lifecycle_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ArtifactLifecycleActionDependencies(get=_get, post=_post, patch=_patch),
        )

    elif name in SKILL_ROUTING_ACTIONS:
        return await execute_skill_routing_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=SkillRoutingActionDependencies(get=_get, post=_post),
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

    elif name in HANDOFF_ACTIONS:
        return await execute_handoff_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=HandoffActionDependencies(
                post=_post,
                get=_get,
                build_handoff_context_summary=_build_handoff_context_summary,
                build_handoff_context_refs=_build_handoff_context_refs,
                summarize_ref_counts=_summarize_handoff_ref_counts,
                format_scope=_format_handoff_scope,
                format_background_payload=_format_handoff_background_payload,
                extract_handoff_field=_extract_handoff_field,
                sanitize_content_preview=_sanitize_handoff_content_preview,
                format_workspace_summary=_format_handoff_workspace_summary,
                format_decomposition=_format_handoff_decomposition,
                format_created_task_packets=_format_created_task_packets,
                format_route_task_packet_execution=_format_route_task_packet_execution,
                format_dispatch_background_task_packet=_format_dispatch_background_task_packet,
                format_reconcile_background_task_packet=_format_reconcile_background_task_packet,
            ),
        )

    elif name in SKILL_ROUTING_ACTIONS:
        return await execute_skill_routing_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=SkillRoutingActionDependencies(get=_get, post=_post),
        )
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
        tier_icon = {"skill": "⚡", "local": "🏠", "cloud": "�?�️", "reference": "📞"}.get(data["tier"], "?")
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
        return await execute_artifact_lifecycle_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ArtifactLifecycleActionDependencies(
                get=_get,
                post=_post,
                patch=_patch,
                annotate_payload=_annotate_structured_tool_payload,
            ),
        )

    elif name == "list_artifacts":
        return await execute_artifact_lifecycle_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ArtifactLifecycleActionDependencies(get=_get, post=_post, patch=_patch),
        )
    elif name == "mailbox_state":
        data = await build_mailbox_state_response(
            args=args,
            session_id=session_id,
            dependencies=MailboxReadDependencies(get_session_identity_defaults=_get_session_identity_defaults),
        )
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "mailbox_submit":
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        data = await _build_mailbox_submit_packet(args=args, payload=payload, api_base=api_base, session_id=session_id)
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "mailbox_get":
        data = await _resolve_mailbox_public_ref(api_base, args)
        if data is None:
            data = await build_mailbox_get_response(
                args=args,
                session_id=session_id,
                dependencies=MailboxReadDependencies(get_session_identity_defaults=_get_session_identity_defaults),
            )
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name in {"reconcile_completed_checkpoints", "review_completed_checkpoint_scope", "review_completed_checkpoint_scopes"}:
        return await execute_artifact_lifecycle_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ArtifactLifecycleActionDependencies(
                get=_get,
                post=_post,
                patch=_patch,
                annotate_payload=_annotate_structured_tool_payload,
            ),
        )
    elif name == "list_open_tasks":
        query = build_list_open_tasks_query(args)
        data = await _get(api_base, f"/artifacts?{query}")
        data = _annotate_open_tasks_with_claims(data, args)
        data = _annotate_open_tasks_with_assignment_safety(data, args)
        return format_list_open_tasks_response(data)
    elif name == "normalize_mcp_intent":
        payload = build_normalize_mcp_intent_payload(args)
        data = await _normalize_mcp_intent(payload["intent"], project_id=payload["project_id"], top_n=payload["top_n"])
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "project_work":
        data = await _build_project_work_payload(api_base, args, session_id=session_id)
        data = _annotate_structured_tool_payload(name, data)
        if _wants_route_diagnostic(args):
            return _format_route_diagnostic(data)
        if _wants_route_answer(args):
            return _format_route_answer(data)
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
        data = _annotate_structured_tool_payload(
            name,
            {
                "status": "error",
                "error": "tool_removed_use_pull_task_context",
                "next_safe_action": "Call pull_task_context instead.",
            },
        )
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "pull_task_context":
        data = await _build_pull_task_context_payload(api_base, args)
        data = _annotate_structured_tool_payload(name, data)
        return format_pull_task_context_response(data)

    elif name == "draft_task_checkpoint":
        from app.dependencies import get_llm_gateway
        from app.services.memory_scribe_service import draft_task_checkpoint

        data = await draft_task_checkpoint(args, get_llm_gateway())
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "record_work_result":
        target = await _resolve_work_result_target(api_base, args)
        project = target["project"]
        if target.get("task_id"):
            lease_guard = _task_mutation_requires_owned_claim(
                project=project,
                task_id=str(target["task_id"]),
                owner_agent=str(args.get("owner_agent") or args.get("agent_id") or args.get("acted_by") or "codex"),
                owner_session_id=str(args.get("session_id") or session_id or ""),
                tool_name=name,
                work_token=str(args.get("work_token") or ""),
                danger_mode=bool(args.get("danger_mode", False)),
                danger_confirmation=str(args.get("danger_confirmation") or ""),
            )
            if lease_guard:
                lease_guard = _annotate_structured_tool_payload(name, lease_guard)
                return json.dumps(lease_guard, indent=2, ensure_ascii=False)
        summary = str(args["summary"]).strip()
        title = str(args.get("title") or "Agent work result").strip() or "Agent work result"
        changed_files = _string_list_arg(args.get("changed_files"))
        verification = _string_list_arg(args.get("verification"))
        decisions = _string_list_arg(args.get("decisions"))
        blockers = _string_list_arg(args.get("blockers"))
        remaining_risk = _string_list_arg(args.get("remaining_risk"))
        next_step = str(args.get("next_step") or "").strip()
        source = str(args.get("source") or "record_work_result").strip() or "record_work_result"
        agent_id = str(args.get("agent_id") or args.get("acted_by") or "codex").strip() or "codex"

        memory_lines = [title, "", summary]
        if changed_files:
            memory_lines.append("Changed files: " + ", ".join(changed_files))
        if verification:
            memory_lines.append("Verification: " + "; ".join(verification))
        if decisions:
            memory_lines.append("Decisions: " + "; ".join(decisions))
        if blockers:
            memory_lines.append("Blockers: " + "; ".join(blockers))
        if remaining_risk:
            memory_lines.append("Remaining risk: " + "; ".join(remaining_risk))
        if next_step:
            memory_lines.append("Next step: " + next_step)
        memory_payload = {
            "content": "\n".join(memory_lines),
            "agent_id": agent_id,
            "memory_type": "task",
            "category": "work_result",
            "project": project,
            "importance_score": float(args.get("importance_score") or 0.65),
            "source": source,
            "tags": [
                "work_result",
                f"project:{project}",
                f"target:{target['target_source']}",
            ],
        }
        if target.get("task_id"):
            memory_payload["tags"].append(f"task_id:{target['task_id']}")
        memory_result = await _post(api_base, "/memories", memory_payload)

        created_issue = None
        clerk_draft = None
        checkpoint_result = None
        resolve_result = None
        route = ["memory"]
        warnings: list[str] = []

        if not target.get("task_id") and bool(args.get("create_issue_if_unmatched", False)):
            source_memory_tag = f"source_memory:{memory_result.get('id')}" if memory_result.get("id") else ""
            created_issue = await _post(
                api_base,
                "/improvements",
                {
                    "project": project,
                    "title": title,
                    "description": summary,
                    "agent_id": agent_id,
                    "importance_score": float(args.get("importance_score") or 0.65),
                    "stage": "proposal",
                    "tags": [
                        tag
                        for tag in ("work-result", "mcp-facade", f"project:{project}", source_memory_tag)
                        if tag
                    ],
                },
            )
            route.append("improvement")

        if target.get("task_id"):
            available_spans = _available_stenographer_spans(args, default_project=project, default_task_id=target["task_id"])
            if available_spans:
                try:
                    from app.dependencies import get_llm_gateway
                    from app.services.checkpoint_draft_service import draft_checkpoint_from_spans

                    draft_args = {
                        "project": project,
                        "task_id": target["task_id"],
                        "work_id": str(args.get("work_id") or "").strip(),
                        "agent_id": agent_id,
                        "session_id": str(args.get("session_id") or "").strip(),
                        "stage": str(args.get("stage") or "completed").strip() or "completed",
                        "status": str(args.get("status") or "done").strip() or "done",
                        "reason": str(args.get("reason") or "record_work_result_clerk_draft").strip(),
                        "use_llm": bool(args.get("clerk_use_llm", args.get("use_llm", False))),
                        "preserve_evidence": bool(args.get("preserve_evidence", True)),
                        "limit": int(args.get("clerk_span_limit") or args.get("limit") or 50),
                    }
                    clerk_record = await draft_checkpoint_from_spans(draft_args, get_llm_gateway())
                    clerk_draft = clerk_record.model_dump(mode="json")
                    clerk_draft["mutates_memory"] = False
                    clerk_draft["recommended_next_tool"] = (
                        "approve_checkpoint_draft"
                        if clerk_draft.get("validation_report", {}).get("can_approve")
                        else "revise_checkpoint_draft"
                    )
                    route.append("clerk_draft")
                    warnings.append(
                        "Stenographer spans were available, so record_work_result created a review-only clerk draft instead of writing a direct task checkpoint."
                    )
                    if bool(args.get("should_resolve_artifact", False)):
                        warnings.append("Artifact was not resolved because clerk draft approval is required before lifecycle closure.")
                except Exception as exc:
                    warnings.append(f"Clerk draft failed; falling back to direct checkpoint: {_format_tool_error_brief(exc)}")

            if clerk_draft is not None:
                data = {
                    "status": "drafted",
                    "project": project,
                    "route": route,
                    "target": target,
                    "memory": {"id": memory_result.get("id")},
                    "clerk_draft": {
                        "draft_id": clerk_draft.get("draft_id"),
                        "version": clerk_draft.get("version"),
                        "status": clerk_draft.get("status"),
                        "recommended_next_tool": clerk_draft.get("recommended_next_tool"),
                        "validation_report": clerk_draft.get("validation_report"),
                        "source_span_ids": clerk_draft.get("source_span_ids"),
                    },
                    "checkpoint": None,
                    "created_issue": created_issue,
                    "resolved_artifact": None,
                    "warnings": warnings,
                    "next_action": clerk_draft.get("recommended_next_tool") or "Review clerk draft before persisting checkpoint.",
                }
                data = _annotate_structured_tool_payload(name, data)
                return json.dumps(data, indent=2, ensure_ascii=False)

            stage = str(args.get("stage") or "completed").strip().lower()
            checkpoint_args = {
                "project": project,
                "task_id": target["task_id"],
                "stage": stage,
                "summary": summary,
                "checkpoint_mode": str(args.get("checkpoint_mode") or "standard").strip() or "standard",
                "changed_files": changed_files,
                "verification": verification,
                "decisions": decisions,
                "blockers": blockers,
                "remaining_risk": remaining_risk,
                "next_step": next_step,
                "next_step_scope": str(args.get("next_step_scope") or "none").strip() or "none",
                "status": args.get("status") or ("done" if stage == "completed" else "active"),
                "reason": str(args.get("reason") or "record_work_result closeout").strip(),
                "acted_by": str(args.get("acted_by") or agent_id).strip() or agent_id,
                "source": source,
            }
            checkpoint_payload = build_report_task_checkpoint_payload(checkpoint_args)
            checkpoint_result = await _post(
                api_base,
                f"/project/tasks/{quote(target['task_id'], safe='')}/changes",
                checkpoint_payload,
            )
            route.append("task_checkpoint")
            if checkpoint_result.get("id"):
                checkpoint_result["stage_evidence"] = f"checkpoint:{checkpoint_result['id']}"

            if bool(args.get("should_resolve_artifact", False)):
                artifact_to_resolve = target.get("artifact_key") or f"task:{project}:{target['task_id']}"
                resolve_result = await _post(
                    api_base,
                    f"/artifacts/{quote(artifact_to_resolve, safe='')}/resolve",
                    {
                        "acted_by": str(args.get("acted_by") or agent_id).strip() or agent_id,
                        "action_source": source,
                        "reason": summary,
                    },
                )
                route.append("resolve_artifact")
        elif not created_issue:
            warnings.append(
                "No task_id/artifact_key was provided and no open task could be matched; recorded memory-only result."
            )

        data = {
            "status": "recorded",
            "project": project,
            "route": route,
            "target": target,
            "memory": {"id": memory_result.get("id")},
            "checkpoint": checkpoint_result,
            "created_issue": created_issue,
            "resolved_artifact": resolve_result,
            "warnings": warnings,
            "next_action": (
                "Review unresolved follow-up before resolving artifact."
                if remaining_risk or blockers or next_step
                else "No immediate follow-up recorded."
            ),
        }
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "start_task_session":
        data = await start_task_session_action(
            args=args,
            api_base=api_base,
            session_id=session_id,
            dependencies=TaskSessionActionDependencies(
                post=_post,
                get_session_identity_defaults=_get_session_identity_defaults,
            ),
        )
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "finish_task_session":
        data = await finish_task_session_action(
            args=args,
            api_base=api_base,
            session_id=session_id,
            dependencies=TaskSessionActionDependencies(
                post=_post,
                get=_get,
                get_session_identity_defaults=_get_session_identity_defaults,
            ),
        )
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name in {"claim_task", "heartbeat_task_claim", "release_task_claim", "force_release_task_claim", "list_task_claims"}:
        data = await execute_task_lease_action(
            name=name,
            args=args,
            session_id=session_id,
            dependencies=TaskLeaseActionDependencies(get_session_identity_defaults=_get_session_identity_defaults),
        )
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name in {
        "get_work_session_state",
        "start_work_session",
        "park_work_session",
        "resume_work_session",
        "end_work_session",
        "record_stenographer_span",
        "list_stenographer_spans",
    }:
        data = execute_work_session_action(
            name=name,
            args=args,
            session_id=session_id,
            dependencies=WorkSessionActionDependencies(task_mutation_guard=_task_mutation_requires_owned_claim),
        )
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name in {
        "clerk_draft_report",
        "draft_checkpoint_from_spans",
        "get_checkpoint_draft",
        "revise_checkpoint_draft",
        "approve_checkpoint_draft",
        "reject_checkpoint_draft",
    }:
        from app.dependencies import get_llm_gateway, get_ollama, get_qdrant
        data = await execute_checkpoint_draft_action(
            name=name,
            args=args,
            dependencies=CheckpointDraftActionDependencies(
                llm_gateway=get_llm_gateway(),
                qdrant=get_qdrant(),
                ollama=get_ollama(),
            ),
        )
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name in {"report_task_checkpoint", "record_task_checkpoint"}:
        return await execute_task_checkpoint_action(
            name=name,
            args=args,
            api_base=api_base,
            session_id=session_id,
            dependencies=TaskCheckpointActionDependencies(
                post=_post,
                get=_get,
                task_mutation_guard=_task_mutation_requires_owned_claim,
            ),
        )

    elif name == "reopen_task":
        task_id = str(args["task_id"]).strip()
        payload = build_reopen_task_payload(args)
        data = await _post(api_base, f"/project/tasks/{quote(task_id, safe='')}/reopen", payload)
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name in {"list_tool_families", "tool_family_tools", "tool_explain", "tool_recommend", "tool_feedback"}:
        return await execute_tool_discovery_action(
            name=name,
            args=args,
            api_base=api_base,
            session_id=session_id,
            dependencies=ToolDiscoveryActionDependencies(
                post=_post,
                build_tool_families_payload=_build_tool_families_payload,
                build_family_tools_payload=_build_family_tools_payload,
                build_tool_explanation=_build_tool_explanation,
                build_tool_recommendation=_build_tool_recommendation,
                get_tool_stage=get_tool_stage,
                record_tool_feedback=record_tool_feedback,
                annotate_payload=_annotate_structured_tool_payload,
            ),
        )

    elif name in {"resolve_artifact", "reopen_artifact"}:
        return await execute_artifact_lifecycle_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ArtifactLifecycleActionDependencies(get=_get, post=_post, patch=_patch),
        )

    elif name == "memory_health":
        return await execute_runtime_utility_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=RuntimeUtilityActionDependencies(get=_get, post=_post),
        )

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
            "EXPERT HELPER GUIDANCE:\n"
            "  Public surface first: help, state, get, submit.\n"
            "  Do not bootstrap from mcp_settings.json, alwaysAllow, client allowlists, or cached full tool lists.\n"
            "  Start project work with state for the current public workflow packet.\n"
            "  Use get for public refs/read-only questions and submit for public forms before falling back to specialized tools.\n"
            "  Use ask_project/project_work only when state/get/help directs a facade fallback or for natural human/project questions.\n"
            "  Stay on the compact surface unless you need deep/debug access.\n"
            "  For task continuation, use get with task:<project>:<task_id> or submit get_task_context first; use reopen_task only to reactivate a closed/inactive task.\n"
            "  Treat runtime details such as Docker test contours as project-specific hints from project context, not universal rules."
        )

        sections.append(
            render_onboarding_instincts_block(
                get_active_operational_instincts(
                    context_type="onboarding",
                    storage_trust_status=locals().get("trust_status", "ok"),
                    limit=8,
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

    elif name in {"load_instruction_layer", "list_instruction_layers"}:
        return await execute_runtime_utility_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=RuntimeUtilityActionDependencies(get=_get, post=_post),
        )

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
    elif name == "get_task_execution_context":
        data = await _post(api_base, "/task-execution-context", build_task_execution_context_payload(args))
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "operational_tray":
        context_payload = build_operational_tray_context_payload(args)
        context = await _post(api_base, "/task-execution-context", context_payload)
        action = str(args.get("action") or "inspect").strip().lower()
        if action == "inspect":
            rule_packet = _build_operational_rule_packet(context, args)
            data = {
                "project": context.get("project"),
                "state": context.get("state"),
                "task": context.get("task"),
                "readiness": context.get("readiness") or {},
                "operation_tray": context.get("operation_tray") or {},
                "required_rules": context.get("required_rules") or [],
                "recommended_rules": context.get("recommended_rules") or [],
                "rule_packet": rule_packet,
                "facade": {
                    "allowed_actions": [
                        "record_stage_evidence",
                        "record_checkpoint",
                        "draft_checkpoint",
                        "review_rule_candidates",
                        "list_rule_candidates",
                    ],
                    "catalog_hidden": True,
                },
            }
            data = _annotate_structured_tool_payload(name, data)
            return json.dumps(data, indent=2, ensure_ascii=False)

        if action != "execute":
            raise ValueError("operational_tray action must be inspect or execute")

        tray_action = str(args.get("tray_action") or args.get("tool") or "").strip()
        action_args = args.get("args")
        if not isinstance(action_args, dict) or not action_args:
            action_args = args.get("arguments") or {}
        if not isinstance(action_args, dict):
            action_args = {}
        readiness = context.get("readiness") or {}
        ready_to_enter = bool(readiness.get("ready_to_enter", True))
        evidence_actions = {"record_stage_evidence", "record_checkpoint", "draft_checkpoint"}
        if not ready_to_enter and tray_action not in evidence_actions:
            data = {
                "blocked": True,
                "reason": "Readiness gate blocked the requested tray action.",
                "readiness": readiness,
                "operation_tray": context.get("operation_tray") or {},
                "next_allowed_actions": sorted(evidence_actions),
            }
            data = _annotate_structured_tool_payload(name, data)
            return json.dumps(data, indent=2, ensure_ascii=False)
        if bool(args.get("dry_run", False)):
            data = {
                "dry_run": True,
                "tray_action": tray_action,
                "readiness": readiness,
                "operation_tray": context.get("operation_tray") or {},
                "would_execute": _operational_tray_target_tool(tray_action),
            }
            data = _annotate_structured_tool_payload(name, data)
            return json.dumps(data, indent=2, ensure_ascii=False)

        base_args = {
            "project": args.get("project", "mnemoforge"),
            "task_id": args.get("task_id", ""),
        }
        work_token = str(args.get("work_token") or "").strip()
        if tray_action == "record_stage_evidence":
            stage = str(action_args.get("stage") or _checkpoint_stage_for_state(str(args["state"]))).strip()
            checkpoint_args = {
                **base_args,
                **action_args,
                "stage": stage,
                "summary": action_args.get("summary") or f"Stage evidence recorded for {args['state']}.",
                "checkpoint_mode": action_args.get("checkpoint_mode") or "lightweight",
                "source": action_args.get("source") or "operational_tray",
                "acted_by": action_args.get("acted_by") or "codex",
                "session_id": action_args.get("session_id") or args.get("session_id", ""),
                "work_token": work_token,
            }
            return await _execute_tool("record_task_checkpoint", checkpoint_args, api_base, session_id=session_id)
        if tray_action == "record_checkpoint":
            checkpoint_args = {
                **base_args,
                **action_args,
                "stage": action_args.get("stage") or _checkpoint_stage_for_state(str(args["state"])),
                "summary": action_args.get("summary") or f"Checkpoint recorded for {args['state']}.",
                "source": action_args.get("source") or "operational_tray",
                "acted_by": action_args.get("acted_by") or "codex",
                "session_id": action_args.get("session_id") or args.get("session_id", ""),
                "work_token": work_token,
            }
            return await _execute_tool("record_task_checkpoint", checkpoint_args, api_base, session_id=session_id)
        if tray_action == "draft_checkpoint":
            draft_args = {
                **action_args,
                "project": args.get("project", "mnemoforge"),
                "task_id": args.get("task_id", action_args.get("task_id", "")),
                "work_id": action_args.get("work_id") or args.get("work_id", ""),
                "agent_id": action_args.get("agent_id") or args.get("agent_id", "codex"),
                "session_id": action_args.get("session_id") or args.get("session_id", ""),
                "task_title": action_args.get("task_title") or str(args.get("task") or "")[:160],
                "raw_notes": action_args.get("raw_notes") or str(args.get("task") or ""),
                "stage": action_args.get("stage") or _checkpoint_stage_for_state(str(args["state"])),
                "status": action_args.get("status") or "active",
            }
            return await _execute_tool("clerk_draft_report", draft_args, api_base, session_id=session_id)
        if tray_action == "review_rule_candidates":
            review_args = {
                "project": args.get("project", "mnemoforge"),
                "status": action_args.get("status", "candidate"),
                "source_task_id": action_args.get("source_task_id") or args.get("task_id", ""),
                "limit": action_args.get("limit", 100),
                "max_matches": action_args.get("max_matches", 5),
            }
            return await _execute_tool("get_rule_candidate_review_packet", review_args, api_base, session_id=session_id)
        if tray_action == "list_rule_candidates":
            list_args = {
                "project": args.get("project", "mnemoforge"),
                "status": action_args.get("status"),
                "source_task_id": action_args.get("source_task_id") or args.get("task_id", ""),
                "limit": action_args.get("limit", 100),
            }
            return await _execute_tool("list_rule_candidates", list_args, api_base, session_id=session_id)
        raise ValueError(f"Unsupported operational_tray tray_action: {tray_action}")
    elif name == "upsert_knowledge_tree_node":
        data = await _post(api_base, "/tree/upsert-by-path", build_upsert_knowledge_tree_node_payload(args))
        data = _annotate_structured_tool_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    elif name == "get_project_readiness":
        data = await _post(api_base, "/project/readiness", build_project_readiness_payload(args))
        return format_project_readiness_response(data)
    elif name == "get_project_bootstrap_checklist":
        data = await _post(api_base, "/project/bootstrap-checklist", build_project_bootstrap_payload(args))
        return format_project_bootstrap_response(data)
    elif name == "get_project_reconstruction_bundle":
        data = await _post(api_base, "/project/reconstruction-bundle", build_project_reconstruction_payload(args))
        return format_project_reconstruction_response(data)
    elif name == "plan_remote_snapshot":
        data = await _post(api_base, "/project/remote-snapshot/plan", build_remote_snapshot_payload(args))
        return format_remote_snapshot_plan_response(data)
    elif name == "sync_remote_snapshot":
        data = await _post(api_base, "/project/remote-snapshot/sync", build_remote_snapshot_payload(args))
        return format_remote_snapshot_sync_response(data)
    elif name == "get_storage_trust_status":
        data = await _get(api_base, "/admin/storage-trust")
        return format_storage_trust_response(data)

    elif name in COORDINATION_ACTIONS:
        return await execute_coordination_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=CoordinationActionDependencies(get=_get, post=_post),
        )

    elif name == "get_task_status":
        return await execute_runtime_utility_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=RuntimeUtilityActionDependencies(get=_get, post=_post),
        )

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
        recovery = await _recover_unknown_tool_call(name, args)
        if recovery:
            recovered_tool = str(recovery.get("tool") or "").strip()
            recovered_args = recovery.get("args")
            if recovered_tool and recovered_tool != name and isinstance(recovered_args, dict):
                return await _execute_tool(recovered_tool, recovered_args, api_base, session_id=session_id)
        raise ValueError(_unknown_tool_error_message(name, args))


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
                      "operational_tray",
            "list_project_laws", "get_project_law",
                      "project_context", "project_verify", "project_capture",
                      "project_rules", "project_rule_candidates_from_stenography", "list_rule_candidates", "get_rule_candidate_review_packet", "review_rule_candidate", "promote_rule_candidate", "revise_law_from_rule_candidate",
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
        init_params = msg.get("params", {}) if isinstance(msg.get("params"), dict) else {}
        client_info = init_params.get("clientInfo", {})
        agent_name = client_info.get("name", "") or ""
        # Normalise: "Claude Code" → "claude-code", "Codex CLI" → "codex"
        agent_id = agent_name.lower().replace(" ", "-") if agent_name else None
        inferred_modes = _infer_small_context_modes(init_params)
        requested_tool_catalog_mode = _extract_requested_tool_catalog_mode(init_params)
        requested_context_hygiene_mode = _extract_requested_context_hygiene_mode(init_params)
        negotiated_tool_catalog_mode = requested_tool_catalog_mode or str(inferred_modes.get("tool_catalog_mode") or "") or _default_tool_catalog_mode()
        negotiated_context_hygiene_mode = requested_context_hygiene_mode or str(inferred_modes.get("context_hygiene_mode") or "")
        runtime_profile_id = _extract_runtime_profile_id(init_params, inferred_modes)
        model_name = _extract_model_name(init_params)
        agent_fingerprint = ""
        try:
            from app.services.mcp_agent_identity import build_fingerprint_from_identity, load_or_create_agent_identity

            identity = load_or_create_agent_identity(
                client_name=agent_name or "unknown-client",
                runtime_profile_id=runtime_profile_id,
            )
            agent_fingerprint = build_fingerprint_from_identity(
                identity,
                workspace_root=os.getenv("MNEMOFORGE_WORKSPACE_ROOT") or os.getcwd(),
                client_name=agent_name or "",
                model_name=model_name,
                runtime_profile_id=runtime_profile_id,
            )
        except Exception:
            agent_fingerprint = ""
        inferred_context_mode = bool(inferred_modes.get("reason")) and (
            not requested_tool_catalog_mode or not requested_context_hygiene_mode
        )

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
                "tool_catalog_mode": negotiated_tool_catalog_mode,
                "context_hygiene_mode": negotiated_context_hygiene_mode,
                "runtime_profile_id": runtime_profile_id,
                "agent_fingerprint": agent_fingerprint,
                "model_name": model_name,
            })

        result: dict = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mnemoforge", "version": "1.0.0"},
        }
        if agent_id:
            result["_mnemoforge"] = build_mnemoforge_initialize_hint(agent_id)
            result["_supermemory"] = result["_mnemoforge"]
            if negotiated_tool_catalog_mode:
                result["_mnemoforge"]["tool_catalog"]["negotiated_mode"] = negotiated_tool_catalog_mode
                if inferred_context_mode and not requested_tool_catalog_mode:
                    result["_mnemoforge"]["tool_catalog"]["inferred"] = True
                    result["_mnemoforge"]["tool_catalog"]["inference_reason"] = inferred_modes.get("reason")
            if negotiated_context_hygiene_mode:
                result["_mnemoforge"]["context_hygiene"] = {
                    "negotiated_mode": negotiated_context_hygiene_mode,
                    "small_context_behavior": "Tool call JSON responses omit service/debug budget keys from the main payload and expose refs for full/debug replay.",
                    "full_request": {"arguments": {"context_hygiene_mode": "full"}},
                }
                if inferred_context_mode and not requested_context_hygiene_mode:
                    result["_mnemoforge"]["context_hygiene"]["inferred"] = True
                    result["_mnemoforge"]["context_hygiene"]["inference_reason"] = inferred_modes.get("reason")
                if inferred_modes.get("model_context_window"):
                    result["_mnemoforge"]["context_hygiene"]["model_context_window"] = inferred_modes.get("model_context_window")
            result["_mnemoforge"]["agent_identity"] = {
                "agent_id": agent_id,
                "agent_fingerprint": agent_fingerprint,
                "runtime_profile_id": runtime_profile_id,
                "model_name": model_name,
                "claim_defaults": {
                    "agent_fingerprint": agent_fingerprint,
                    "runtime_profile_id": runtime_profile_id,
                },
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    elif method in ("initialized", "notifications/initialized"):
        return None  # notification — no response

    elif method == "tools/list":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if session_id and not _normalize_tool_catalog_mode(params.get("mode") or params.get("catalog_mode")):
            try:
                from app.services.mcp_session_store import get_session_store

                ctx = await get_session_store().get_context(session_id)
                negotiated_mode = _normalize_tool_catalog_mode((ctx or {}).get("tool_catalog_mode"))
                if negotiated_mode:
                    params = {**params, "mode": negotiated_mode}
            except Exception:
                pass
        return {"jsonrpc": "2.0", "id": req_id, "result": _tools_list_payload(params)}

    elif method == "tools/call":
        params = msg.get("params", {})
        try:
            call_args = params.get("arguments", {}) if isinstance(params.get("arguments", {}), dict) else {}
            result_text = await _execute_tool(
                params.get("name", ""), call_args, api_base, session_id
            )
            context_hygiene_mode = _extract_requested_context_hygiene_mode(call_args)
            if not context_hygiene_mode and session_id:
                try:
                    from app.services.mcp_session_store import get_session_store

                    ctx = await get_session_store().get_context(session_id)
                    context_hygiene_mode = _normalize_context_hygiene_mode((ctx or {}).get("context_hygiene_mode"))
                except Exception:
                    context_hygiene_mode = ""
            result_text = _sanitize_tool_result_for_context(result_text, context_hygiene_mode=context_hygiene_mode)
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



