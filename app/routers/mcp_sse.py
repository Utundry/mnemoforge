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
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.services.mcp_tool_contracts import (
    build_enrich_task_payload,
    build_task_execution_context_payload,
    build_list_open_tasks_query,
    build_normalize_mcp_intent_payload,
    build_project_workflow_payload,
    build_project_workflow_submit_payload,
    build_project_workflow_submit_plan,
    build_project_readiness_payload,
    build_reopen_task_payload,
    build_mnemoforge_initialize_hint,
    build_mnemoforge_onboarding_basics,
    build_report_task_checkpoint_payload,
    format_list_open_tasks_response,
    format_task_checkpoint_response,
    format_pull_task_context_response,
    format_enrich_task_response,
    format_project_workflow_response,
    format_project_workflow_submit_response,
    format_project_readiness_response,
    sync_tool_definitions,
    tool_definition,
)
from app.services.mcp_tool_registry import get_tool_stage, observe_tool_use, record_tool_feedback, tool_feedback_expected
from app.services.replay_completeness_service import build_replay_drill_decision, build_token_budget, evaluate_execution_readiness, evaluate_replay_completeness
from app.services.stenography_protocol_service import build_stenography_coverage, build_stenography_protocol
from app.services.route_pattern_store import get_route_pattern_store
from app.services.unified_artifact_service import task_has_closeout_evidence
from app.services.context_page_store import compact_page, get_context_page_store
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
    artifact_list_status_filter,
    artifact_topic_query,
    build_simple_get_query_response,
    build_simple_public_ref_response,
    explicit_artifact_list_type,
)
from app.services.mcp_simple_surface_actions import (
    SimpleSurfaceDependencies,
    build_simple_get_response,
    build_simple_help_response,
    build_simple_state_response,
    build_simple_submit_response,
)
from app.services.mcp_response_filter import filter_mcp_response, response_profile_from_args
from app.services.mcp_lifecycle_receipts import build_lifecycle_receipt, public_auto_work_session_payload
from app.services.mcp_project_work_routing import (
    project_work_route_with_backend as _project_work_route_with_backend,
)
from app.services.project_identity_service import project_identity_envelope, resolve_project_id
from app.services.public_diagnostic_service import build_public_diagnostic_incident
from app.services.mcp_project_work_results import (
    project_work_action_card as _project_work_action_card,
    weak_model_mutation_guardrail as _weak_model_mutation_guardrail,
)
from app.services.mcp_project_work_facade_helpers import (
    project_work_lease_conflict_recovery_packet as _project_work_lease_conflict_recovery_packet,
    project_work_maintenance_suggestion as _project_work_maintenance_suggestion,
    redact_project_work_submit_payload as _redact_project_work_submit_payload,
    sanitize_project_work_result as _sanitize_project_work_result,
)
from app.services.mcp_project_context_rules import project_context_rule_refs as _project_context_rule_refs
from app.services.mcp_open_work_items import (
    annotate_open_tasks_with_assignment_safety as _open_work_annotate_assignment_safety,
    annotate_open_tasks_with_claims as _open_work_annotate_claims,
    prepare_open_work_items as _open_work_prepare_items,
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
from app.services.server_build_info import public_server_build_info, server_build_diagnostics_enabled
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
from app.services.mcp_workflow_specs import load_named_json_spec, load_route_catalog_spec, load_tool_family_registry, load_tool_surface_spec
from app.services.data_hygiene_service import build_maintenance_suggestion
from app.services.mcp_handoff_actions import (
    HANDOFF_ACTIONS,
    HandoffActionDependencies,
    execute_handoff_action,
)
from app.services.mcp_handoff_formatters import (
    _build_handoff_context_refs,
    _build_handoff_context_summary,
    _summarize_handoff_ref_counts,
    _summarize_handoff_bucket_counts,
    _format_handoff_merge_back_guidance,
    _format_handoff_scope,
    _format_handoff_background_payload,
    _append_handoff_background_state,
    _extract_handoff_field,
    _sanitize_handoff_content_preview,
    _format_handoff_workspace_summary,
    _format_handoff_decomposition,
    _format_created_task_packets,
    _format_route_task_packet_execution,
    _format_dispatch_background_task_packet,
    _format_reconcile_background_task_packet,
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
from app.services.mcp_onboarding_actions import (
    ONBOARDING_ACTIONS,
    OnboardingActionDependencies,
    execute_onboarding_action,
)
from app.services.mcp_project_knowledge_actions import (
    PROJECT_KNOWLEDGE_ACTIONS,
    ProjectKnowledgeActionDependencies,
    execute_project_knowledge_action,
)
from app.services.mcp_work_result_actions import (
    WorkResultActionDependencies,
    execute_work_result_action,
)
from app.services.mcp_facade_backend_routing import (
    FacadeBackendRoutingDependencies,
    facade_route_with_backend,
)
from app.services.mcp_facade_route_builders import (
    PROJECT_CAPTURE_ROUTE_CATALOG as _PROJECT_CAPTURE_ROUTE_CATALOG,
    PROJECT_CONTEXT_ROUTE_CATALOG as _PROJECT_CONTEXT_ROUTE_CATALOG,
    PROJECT_VERIFY_ROUTE_CATALOG as _PROJECT_VERIFY_ROUTE_CATALOG,
    project_capture_route as _project_capture_route,
    project_context_route as _project_context_route,
    project_verify_route as _project_verify_route,
)
from app.services.mcp_session_auto_record import auto_record_session
from app.services.mcp_pull_task_context import (
    PullTaskContextDependencies,
    build_pull_task_context_payload,
)
from app.services.mcp_semantic_rule_packet import build_semantic_rule_packet
from app.services.mcp_route_formatters import (
    build_route_telemetry as _build_route_telemetry,
    diagnostic_value as _diagnostic_value,
    facade_action_card as _facade_action_card,
    format_route_answer as _format_route_answer,
    format_route_diagnostic as _format_route_diagnostic,
    selected_route_public as _selected_route_public,
    wants_route_answer as _wants_route_answer,
    wants_route_diagnostic as _wants_route_diagnostic,
)
from app.services.mcp_sse_tool_catalog import (
    TOOLS,
    _find_tool_definition,
    _normalized_tool_name,
    _unknown_tool_recovery_pattern,
    _unknown_tool_call_args,
    _unknown_tool_llm_recovery,
    _recover_unknown_tool_call,
    _unknown_tool_error_message,
    _infer_tool_family,
    _tool_catalog,
    _tool_surface_role,
    _tool_surface_guidance,
    _annotate_tool_surface,
    _annotated_tool_catalog,
    _summarize_input_schema,
    _compact_tool_catalog,
    _default_tool_catalog_mode,
    _tools_list_payload,
    _normalize_tool_catalog_mode,
    _normalize_context_hygiene_mode,
    _mnemoforge_params,
    _extract_requested_tool_catalog_mode,
    _extract_requested_context_hygiene_mode,
    _int_from_nested,
    _casefold_nested,
    _infer_small_context_modes,
    _extract_runtime_profile_id,
    _extract_model_name,
    _extract_project_id,
    _family_tools,
    _family_spec,
    _family_recommendation_score,
    _tool_lifecycle_annotations,
    _annotate_structured_tool_payload,
    _build_tool_feedback_envelope,
    _recommend_family_order,
    _build_tool_families_payload,
    _build_family_tools_payload,
    _build_tool_explanation,
    _tool_input_schema,
    _tool_example_payload,
    _looks_like_reactivation_intent,
    _looks_like_checkpoint_resume_intent,
    _normalize_mcp_intent_lexical,
    _normalize_mcp_intent_llm,
    _normalize_mcp_intent,
    _route_tokens,
    _route_catalog_scores,
    _selected_catalog_route,
    _render_route_payload_template,
    _learned_payload_from_decision,
    _route_topic_intent,
    _route_needs_llm_disambiguation,
    _catalog_route_by_intent,
    _extract_task_id_from_text,
    _is_full_uuid,
    _extract_task_id_like_from_text,
    _learned_route_match,
    _record_learned_route_pattern,
    _invalidate_conflicting_learned_route,
    _extract_json_object,
    _build_tool_recommendation,
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
        "project_id": str(ctx.get("project_id") or ctx.get("project") or "").strip(),
    }


def _public_simple_tool_payload(tool_name: str, data: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "get" and str(args.get("response_format") or "").strip().lower() == "context":
        return data
    profile = response_profile_from_args(args)
    if _wants_route_diagnostic(args) or server_build_diagnostics_enabled():
        profile = "diagnostic"
    annotated = _annotate_structured_tool_payload(
        tool_name,
        data,
        include_server_build=_wants_route_diagnostic(args),
    )
    filtered = filter_mcp_response(annotated, profile=profile)
    return filtered if isinstance(filtered, dict) else {}


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
        get=_get,
        get_session_identity_defaults=_get_session_identity_defaults,
        resolve_public_ref=_resolve_mailbox_public_ref,
        resolve_query=lambda base, query_args, sid: build_simple_get_query_response(
            api_base=base,
            args=query_args,
            session_id=sid,
            dependencies=SimpleReadDependencies(
                get=_get,
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


async def _build_simple_state_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return await build_simple_state_response(
        api_base=api_base,
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
        "result": _sanitize_project_work_result(result, args),
        "route_telemetry": route_telemetry,
        "warnings": warnings,
        "next_safe_action": "Continue from the executed rule route result." if executed else "Review submit_payload, then call agent_action.recommended_next_call exactly; do not call memory_store.",
    }


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
    if task_id_like or bool(lexical_route.get("structural_match")):
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
        args=args,
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
        "SloplessCode ask_project diagnostic",
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
            "next_safe_action": semantic_rules.get("next_safe_action") or "Satisfy rule preconditions before execution.",
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
        "result": _sanitize_project_work_result(result, args),
        "semantic_rules": semantic_rules,
        "route_telemetry": route_telemetry,
        "warnings": warnings,
        "next_safe_action": "Continue from the executed route result." if executed else "Review submit_payload before confirming mutation.",
    }


async def _facade_route_with_backend(
    *,
    facade: str,
    args: dict[str, Any],
    catalog: tuple[dict[str, Any], ...],
    route_fn,
) -> dict[str, Any]:
    return await facade_route_with_backend(
        facade=facade,
        args=args,
        catalog=catalog,
        route_fn=route_fn,
        dependencies=FacadeBackendRoutingDependencies(
            invalidate_conflicting_learned_route=_invalidate_conflicting_learned_route,
            learned_route_match=_learned_route_match,
            route_needs_llm_disambiguation=_route_needs_llm_disambiguation,
            llm_disambiguate=_facade_llm_disambiguate,
            record_learned_route_pattern=_record_learned_route_pattern,
            format_error=lambda exc: _format_tool_error_brief(exc, default="llm disambiguation failed"),
        ),
    )


async def _build_project_context_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    route = await _facade_route_with_backend(
        facade="project_context",
        args=args,
        catalog=_PROJECT_CONTEXT_ROUTE_CATALOG,
        route_fn=_project_context_route,
    )
    return await _run_facade_route(facade="project_context", route=route, args=args, api_base=api_base, session_id=session_id)


async def _build_project_verify_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    route = await _facade_route_with_backend(
        facade="project_verify",
        args=args,
        catalog=_PROJECT_VERIFY_ROUTE_CATALOG,
        route_fn=_project_verify_route,
    )
    data = await _run_facade_route(facade="project_verify", route=route, args=args, api_base=api_base, session_id=session_id)
    if route["intent_type"] in {"verification_context", "restart_validation_plan"}:
        project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
        state = str(route.get("payload", {}).get("state") or args.get("state") or "verification").strip() or "verification"
        data["project_verify_guidance"] = {
            "verification_contour_ref": f"verification_contour:{project}:{state}",
            "next_safe_action": "Run only checks allowed by the returned verification contour, then record evidence through the project lifecycle.",
        }
    return data


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




async def _build_project_work_payload(api_base: str, args: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    allow_mutation = bool(args.get("allow_mutation", False))
    route = await _project_work_route_with_backend(args)
    warnings: list[str] = []
    executed = False
    result: Any = None
    semantic_rules = _build_semantic_rule_packet(facade="project_work", route=route, args=args)
    lease_guard: dict[str, Any] | None = None
    auto_work_session: dict[str, Any] | None = None
    dispatch_allowed = True
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
            work_handle=str(route_payload.get("work_handle") or args.get("work_handle") or ""),
            danger_mode=bool(args.get("danger_mode", False)),
            danger_confirmation=str(args.get("danger_confirmation") or ""),
        )

    if route["mutating"] and not allow_mutation:
        dispatch_allowed = False
        warnings.append(
            "Selected route is mutating; project_work returned a route plan. Set allow_mutation=true only after reviewing the payload and guardrails."
        )
    elif semantic_rules.get("blocked"):
        warnings.append("Semantic rule precondition failed; selected route execution was blocked.")
        result = {
            "status": "conflict",
            "error": semantic_rules.get("block_error") or "rule_precondition_failed",
            "semantic_rules": semantic_rules,
            "next_safe_action": semantic_rules.get("next_safe_action") or "Satisfy semantic rule preconditions before mutation.",
        }
        executed = True
    elif lease_guard:
        if route["tool"] == "record_work_result" and _can_auto_start_checkpoint_session(lease_guard=lease_guard, args=args):
            auto_start = await _auto_start_checkpoint_work_session(
                api_base=api_base,
                project=str((route.get("payload") or {}).get("project") or args.get("project") or "mnemoforge"),
                task_id=str((route.get("payload") or {}).get("task_id") or args.get("task_id") or ""),
                args=args,
                session_id=session_id,
                source="project_work.record_work_result",
            )
            if str(auto_start.get("status") or "") == "started":
                auto_work_session = public_auto_work_session_payload(auto_start)
                route_payload = dict(route.get("payload") or {})
                route_payload.update(
                    {
                        "work_handle": auto_start.get("work_handle"),
                        "session_id": auto_start.get("owner_session_id"),
                        "owner_agent": auto_start.get("owner_agent"),
                        "_auto_work_session": auto_work_session,
                    }
                )
                route = {**route, "payload": route_payload}
                lease_guard = None
                warnings.append("Unclaimed task was auto-claimed so checkpoint/result recording can persist.")
        if lease_guard:
            lease_guard = _project_work_lease_conflict_recovery_packet(
                lease_guard=lease_guard,
                route=route,
                args=args,
            )
            result = lease_guard
            executed = True
            warnings.append("Mutation blocked by task lease ownership policy; use public FSM recovery from result.recovery_protocol.")
    if dispatch_allowed and not executed and route["tool"] == "list_open_tasks":
        requested_type = str(route["payload"].get("artifact_type") or route["payload"].get("type") or "all").strip().lower()
        retrieval_limit = (
            max(int(route["payload"].get("limit", 50)), 100)
            if requested_type == "all"
            else int(route["payload"].get("limit", 50))
        )
        retrieval_payload = {**route["payload"], "limit": retrieval_limit}
        query = build_list_open_tasks_query(retrieval_payload)
        result = await _get(api_base, f"/artifacts?{query}")
        result = _open_work_prepare_items(result, limit=int(route["payload"].get("limit", 50)))
        result = _open_work_annotate_claims(result, route["payload"], format_error=lambda exc: _format_tool_error_brief(exc))
        result = _open_work_annotate_assignment_safety(result, route["payload"])
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "list_closeable_completed_tail":
        result_text = await _execute_tool("list_closeable_completed_tail", route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        if isinstance(result, dict) and isinstance(result.get("result"), dict):
            result = result["result"]
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "pull_task_context":
        result = await _build_pull_task_context_payload(api_base, route["payload"])
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "start_task_session":
        result_text = await _execute_tool("start_task_session", route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "finish_task_session":
        result_text = await _execute_tool("finish_task_session", route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "get_task_execution_context":
        result = await _post(api_base, "/task-execution-context", build_task_execution_context_payload(route["payload"]))
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "get_project_readiness":
        result = await _post(api_base, "/project/readiness", build_project_readiness_payload(route["payload"]))
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "task_capture_review":
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
    elif dispatch_allowed and not executed and route["tool"] in {"approve_checkpoint_draft", "reject_checkpoint_draft"}:
        if not str(route["payload"].get("draft_id") or "").strip():
            warnings.append("Checkpoint draft approval/rejection requires draft_id.")
        else:
            result_text = await _execute_tool(route["tool"], route["payload"], api_base, session_id=session_id)
            try:
                result = json.loads(result_text)
            except Exception:
                result = result_text
            executed = True
    elif dispatch_allowed and not executed and route["tool"] == "record_work_result":
        result_text = await _execute_tool("record_work_result", route["payload"], api_base, session_id=session_id)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "mailbox_submit":
        submit_args = dict(route["payload"])
        submit_payload = dict(submit_args.get("payload")) if isinstance(submit_args.get("payload"), dict) else {}
        result = await _build_mailbox_submit_packet(
            args=submit_args,
            payload=submit_payload,
            api_base=api_base,
            session_id=session_id,
        )
        executed = True
    elif dispatch_allowed and not executed and route["tool"] == "project_rules":
        result_text = await _execute_tool("project_rules", route["payload"], api_base)
        try:
            result = json.loads(result_text)
        except Exception:
            result = result_text
        executed = True
    elif dispatch_allowed and not executed:
        warnings.append("No confident project-work route was found; use tool_recommend or clarify the intent.")

    next_safe_action = "Review the selected route and execute the submit_payload if it matches the operator intent."
    if lease_guard and isinstance(result, dict) and str(result.get("next_safe_action") or "").strip():
        next_safe_action = str(result["next_safe_action"])
    elif executed:
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
    maintenance_suggestion = _project_work_maintenance_suggestion(route=route, args=args)
    lifecycle_receipt = build_lifecycle_receipt(route_tool=str(route.get("tool") or ""), result=result, warnings=warnings)
    if lifecycle_receipt:
        action_card["receipt"] = lifecycle_receipt

    payload_project = route["payload"].get("project") or route["payload"].get("project_id") or args.get("project") or "mnemoforge"
    payload = {
        "status": "executed" if executed else "planned",
        "action_status": action_card["action_status"],
        "facade": "project_work",
        "project": payload_project,
        "project_identity": project_identity_envelope(
            requested_project=str(args.get("project") or payload_project),
            observed_project=str(payload_project),
        ),
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
        "submit_payload": _redact_project_work_submit_payload(route["payload"]),
        "result": _sanitize_project_work_result(result, args),
        "semantic_rules": semantic_rules,
        "route_telemetry": route_telemetry,
        "compact_result": action_card["compact_result"],
        "warnings": warnings,
        "next_safe_action": next_safe_action,
        "weak_model_guardrail": weak_model_guardrail,
    }
    if lifecycle_receipt:
        payload["receipt"] = lifecycle_receipt
    if isinstance(route.get("claim_filter_resolution"), dict):
        payload["selected_route"]["claim_filter_resolution"] = route["claim_filter_resolution"]
    route_incident = _route_diagnostic_incident_for_payload(facade="project_work", route=route, args=args)
    if route_incident:
        payload["diagnostic_incident"] = route_incident
    if maintenance_suggestion:
        payload["maintenance_suggestion"] = maintenance_suggestion
    return payload


def _route_diagnostic_incident_for_payload(*, facade: str, route: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if not bool(args.get("diagnostic", False)):
        return {}
    candidates = route.get("route_candidates") if isinstance(route.get("route_candidates"), list) else []
    if len(candidates) < 2:
        return {}
    top = candidates[0] if isinstance(candidates[0], dict) else {}
    runner_up = candidates[1] if isinstance(candidates[1], dict) else {}
    try:
        top_score = float(top.get("score") or 0.0)
        next_score = float(runner_up.get("score") or 0.0)
    except (TypeError, ValueError):
        return {}
    if top_score <= 0 or (top_score - next_score) > 0.08:
        return {}
    query_text = str(args.get("intent") or args.get("question") or args.get("query") or "").strip()
    return build_public_diagnostic_incident(
        kind="ambiguous_route_selection",
        safe_next_action="Retry with an explicit facade, task_id, form_id, or expected action; use route_feedback only after confirming a concrete misroute.",
        recommended_next_call={
            "tool": facade,
            "arguments": _compact_public_dict({
                "project": str(args.get("project") or "").strip(),
                "intent": query_text if facade == "project_work" else "",
                "question": query_text if facade == "ask_project" else "",
                "diagnostic": True,
                "scorer_backend": str(args.get("scorer_backend") or "lexical"),
            }),
        },
    )


def _compact_public_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}



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



async def _build_pull_task_context_payload(api_base: str, args: dict[str, Any]) -> dict[str, Any]:
    return await build_pull_task_context_payload(
        api_base,
        args,
        dependencies=PullTaskContextDependencies(get=_get, post=_post),
    )


def _task_mutation_requires_owned_claim(
    *,
    project: str,
    task_id: str,
    owner_agent: str,
    owner_session_id: str,
    tool_name: str,
    work_token: str = "",
    work_handle: str = "",
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
        work_handle=work_handle,
        danger_mode=danger_mode,
        danger_confirmation=danger_confirmation,
    )


def _can_auto_start_checkpoint_session(*, lease_guard: dict[str, Any], args: dict[str, Any]) -> bool:
    return (
        str(lease_guard.get("error") or "") == "active_claim_required"
        and not str(args.get("work_handle") or "").strip()
        and not str(args.get("work_token") or "").strip()
    )


async def _auto_start_checkpoint_work_session(
    *,
    api_base: str,
    project: str,
    task_id: str,
    args: dict[str, Any],
    session_id: str | None,
    source: str,
) -> dict[str, Any]:
    owner_agent = str(args.get("owner_agent") or args.get("agent_id") or args.get("acted_by") or "codex").strip() or "codex"
    auto_session_id = str(args.get("session_id") or session_id or f"auto-checkpoint-{uuid.uuid4().hex[:12]}").strip()
    start_args = {
        "project": project or "mnemoforge",
        "task_id": task_id,
        "owner_agent": owner_agent,
        "agent_id": owner_agent,
        "session_id": auto_session_id,
        "agent_fingerprint": str(args.get("agent_fingerprint") or "").strip(),
        "runtime_profile_id": str(args.get("runtime_profile_id") or "").strip(),
        "summary": "Auto-created work session for task checkpoint recording.",
        "reason": "auto_checkpoint_work_session",
        "source": f"{source}.auto_work_session",
        "checkpoint_mode": "lightweight",
        "auto_heartbeat": False,
        "lease_ttl_seconds": int(args.get("lease_ttl_seconds") or 900),
    }
    return await start_task_session_action(
        args=start_args,
        api_base=api_base,
        session_id=session_id,
        dependencies=TaskSessionActionDependencies(
            post=_post,
            get_session_identity_defaults=_get_session_identity_defaults,
        ),
    )



def _build_semantic_rule_packet(
    *,
    facade: str,
    route: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    return build_semantic_rule_packet(
        facade=facade,
        route=route,
        args=args,
        format_error=lambda exc: _format_tool_error_brief(exc),
    )


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
            patch=_patch,
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
        data = _public_simple_tool_payload(name, data, args)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "state":
        data = await _build_simple_state_payload(api_base, args, session_id=session_id)
        data = _public_simple_tool_payload(name, data, args)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "get":
        data = await _build_simple_get_payload(api_base, args, session_id=session_id)
        data = _public_simple_tool_payload(name, data, args)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name in {"put", "submit"}:
        data = await _build_simple_submit_payload(api_base, args, session_id=session_id, public_tool_name=name)
        data = _public_simple_tool_payload(name, data, args)
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

    elif name in {"improvements_report", "knowledge_hierarchy", "canonicals_by_scope", "set_canonical_status", "merge_canonicals"}:
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
            api_base=api_base,
            dependencies=MailboxReadDependencies(get_session_identity_defaults=_get_session_identity_defaults, get=_get),
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
    elif name in {"list_closeable_completed_tail", "reconcile_completed_checkpoints", "review_completed_checkpoint_scope", "review_completed_checkpoint_scopes"}:
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
        requested_type = str(args.get("artifact_type") or args.get("type") or "all").strip().lower()
        retrieval_limit = max(int(args.get("limit", 50)), 100) if requested_type == "all" else int(args.get("limit", 50))
        retrieval_args = {**args, "limit": retrieval_limit}
        query = build_list_open_tasks_query(retrieval_args)
        data = await _get(api_base, f"/artifacts?{query}")
        data = _open_work_prepare_items(data, limit=int(args.get("limit", 50)))
        data = _open_work_annotate_claims(data, args, format_error=lambda exc: _format_tool_error_brief(exc))
        data = _open_work_annotate_assignment_safety(data, args)
        if isinstance(data, dict):
            data.setdefault(
                "project_identity",
                project_identity_envelope(
                    requested_project=str(args.get("project") or args.get("project_id") or ""),
                    observed_project=str(data.get("project") or args.get("project") or args.get("project_id") or ""),
                ),
            )
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
        return await execute_work_result_action(
            name=name,
            args=args,
            api_base=api_base,
            session_id=session_id,
            dependencies=WorkResultActionDependencies(
                post=_post,
                annotate_payload=_annotate_structured_tool_payload,
                resolve_target=_resolve_work_result_target,
                mutation_requires_owned_claim=_task_mutation_requires_owned_claim,
                can_auto_start_checkpoint_session=_can_auto_start_checkpoint_session,
                auto_start_checkpoint_work_session=_auto_start_checkpoint_work_session,
                available_stenographer_spans=_available_stenographer_spans,
                string_list_arg=_string_list_arg,
                format_error=lambda exc: _format_tool_error_brief(exc),
            ),
        )
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

    elif name in ONBOARDING_ACTIONS:
        return await execute_onboarding_action(
            name=name,
            args=args,
            api_base=api_base,
            session_id=session_id,
            dependencies=OnboardingActionDependencies(
                get=_get,
                post=_post,
                format_error=lambda exc: _format_tool_error_brief(exc, default="skill pack retrieval failed"),
            ),
        )
    elif name in {"load_instruction_layer", "list_instruction_layers"}:
        return await execute_runtime_utility_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=RuntimeUtilityActionDependencies(get=_get, post=_post),
        )

    elif name in PROJECT_KNOWLEDGE_ACTIONS:
        return await execute_project_knowledge_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=ProjectKnowledgeActionDependencies(
                get=_get,
                post=_post,
                annotate_payload=_annotate_structured_tool_payload,
                execute_tool=lambda tool_name, tool_args: _execute_tool(tool_name, tool_args, api_base, session_id=session_id),
            ),
        )
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
    await auto_record_session(ctx, post=_post, build_dialogue_transcript=_build_dialogue_transcript)

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
        project_id = _extract_project_id(init_params)
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
                "project_id": project_id,
            })

        result: dict = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "sloplesscode",
                "version": "1.0.0",
                "compatibilityAliases": ["mnemoforge", "supermemory"],
            },
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

def _internal_api_base(request: Request) -> str:
    from app.config import settings

    server = request.scope.get("server")
    port = int(server[1]) if isinstance(server, (list, tuple)) and len(server) > 1 else settings.server_port
    return f"http://127.0.0.1:{port}{settings.api_prefix.rstrip('/')}"


@router.post("/sse")
async def streamable_http(request: Request):
    """MCP Streamable HTTP transport — accepts JSON-RPC, returns JSON directly."""
    api_base = _internal_api_base(request)

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

    # Tool callbacks stay internal even when Docker publishes a different host port.
    api_base = _internal_api_base(request)

    body = await request.json()
    await _touch_session(sessionId)
    result = await _handle(body, api_base, session_id=sessionId)
    if result is not None:
        await _queue_put(queue, result)
        await _touch_session(sessionId)

    return Response(status_code=202)
