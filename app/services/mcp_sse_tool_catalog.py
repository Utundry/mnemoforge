from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from copy import deepcopy
from typing import Any

from app.services.mcp_tool_contracts import sync_tool_definitions, tool_definition
from app.services.mcp_tool_discovery_actions import build_tool_feedback_envelope
from app.services.mcp_tool_registry import get_tool_stage, tool_feedback_expected
from app.services.mcp_workflow_specs import load_tool_family_registry, load_tool_surface_spec
from app.services.route_pattern_store import get_route_pattern_store
from app.services.server_build_info import public_server_build_info, server_build_diagnostics_enabled


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
                "diagnostic": bool(args.get("diagnostic", False)),
                "response_format": str(args.get("response_format") or "").strip(),
                "source_event_class": str(args.get("source_event_class") or args.get("event_class") or "").strip(),
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


_TOOL_SURFACE_SPEC = load_tool_surface_spec()


_PUBLIC_SURFACE_TOOLS = tuple(_TOOL_SURFACE_SPEC.public_entrypoints)


_COMPATIBILITY_SURFACE_TOOLS = set(_TOOL_SURFACE_SPEC.compatibility_tools)


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


_COMPACT_TOOL_NAMES = tuple(_TOOL_SURFACE_SPEC.compact_tool_names)


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
    for name in _TOOL_SURFACE_SPEC.compact_fill_tools:
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


def _extract_project_id(params: dict[str, Any]) -> str:
    mnemoforge = _mnemoforge_params(params)
    context = mnemoforge.get("context") if isinstance(mnemoforge.get("context"), dict) else {}
    workspace = mnemoforge.get("workspace") if isinstance(mnemoforge.get("workspace"), dict) else {}
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
        mnemoforge.get("project"),
        mnemoforge.get("project_id"),
        context.get("project"),
        context.get("project_id"),
        workspace.get("project"),
        workspace.get("project_id"),
        params.get("project"),
        params.get("project_id"),
        capability_mnemoforge.get("project"),
        capability_mnemoforge.get("project_id"),
        experimental_mnemoforge.get("project"),
        experimental_mnemoforge.get("project_id"),
    ]
    for candidate in candidates:
        project = str(candidate or "").strip()
        if project:
            return project
    return ""


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


def _annotate_structured_tool_payload(
    tool_name: str,
    data: dict[str, Any],
    *,
    include_server_build: bool = False,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        return data
    enriched = deepcopy(data)
    enriched.update(_tool_lifecycle_annotations(tool_name))
    if include_server_build or server_build_diagnostics_enabled():
        enriched["server_build"] = public_server_build_info()
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
    if name == "list_closeable_completed_tail":
        return {"project": project_id or "mnemoforge", "close_policy": "strict", "limit": 100}
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
    elif any(term in lowered for term in ("completed but open", "completed-but-open", "done but still open", "implemented but not closed", "closeable completed tail", "lifecycle anomalies")):
        resolved_tool = "list_closeable_completed_tail"
        resolved_family = "project_knowledge"
        confidence = 0.83
        rationale = "Completed-but-open lifecycle anomaly intent maps to the read-only repair candidate finder."
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
        args={"project": project_id},
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
    if args:
        for route in catalog:
            structural_arg = str(route.get("structural_arg") or "").strip()
            if structural_arg and bool(args.get(structural_arg)):
                structural_candidate = {
                    "intent_type": route["intent_type"],
                    "tool": route["tool"],
                    "score": 1.0,
                    "matched_example": f"arg:{structural_arg}",
                }
                remaining = [item for item in candidates if item.get("intent_type") != route["intent_type"]]
                return route, [structural_candidate, *remaining][:3]
    if not candidates or float(candidates[0].get("score") or 0.0) < min_score:
        return None, candidates[:3]
    best = candidates[0]
    route = next(item for item in catalog if item["intent_type"] == best["intent_type"])
    return route, candidates[:3]


def _render_route_payload_template(
    template: dict[str, Any],
    *,
    args: dict[str, Any],
    project: str,
    intent: str,
    limit: int,
    learned_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    learned = learned_payload if isinstance(learned_payload, dict) else {}
    topic_intent = "" if learned.get("include_query") is False else _route_topic_intent(intent)
    context = {
        "project": project,
        "intent": intent,
        "topic_intent": topic_intent,
        "status_filter": learned.get("status") or args.get("status_filter") or artifact_list_status_filter(intent),
        "artifact_type": learned.get("type") or args.get("artifact_type") or explicit_artifact_list_type(intent),
        "limit": limit,
    }
    for key, raw_value in template.items():
        value = raw_value
        if isinstance(raw_value, str) and raw_value.startswith("{") and raw_value.endswith("}"):
            token = raw_value[1:-1]
            parts = token.split(":")
            name = parts[0]
            if name == "limit":
                default_limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else limit
                max_limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 200
                value = min(max(1, int(args.get("limit") or default_limit)), max_limit)
            elif name in context:
                value = context[name]
            elif name in learned:
                value = learned.get(name)
            else:
                value = args.get(name)
        if value not in (None, "", []):
            rendered[key] = value
    return rendered


def _learned_payload_from_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(decision, dict):
        return {}
    metadata = decision.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    learned_payload = metadata.get("learned_payload")
    if isinstance(learned_payload, dict):
        return learned_payload
    feedback_events = metadata.get("feedback_events")
    if not isinstance(feedback_events, list):
        return {}
    for event in reversed(feedback_events):
        if not isinstance(event, dict):
            continue
        context = event.get("context")
        if not isinstance(context, dict):
            continue
        expected_payload = context.get("expected_payload")
        if isinstance(expected_payload, dict):
            return expected_payload
    return {}


def _route_topic_intent(intent: str) -> str:
    text = str(intent or "").strip()
    normalized = re.sub(r"[_\-/\.]+", " ", text).casefold().strip()
    if normalized in {"list artifacts", "list artifact", "artifacts", "artifact list"}:
        return ""
    return text


def _route_needs_llm_disambiguation(candidates: list[dict[str, Any]]) -> bool:
    if not candidates:
        return True
    top = float(candidates[0].get("score") or 0.0)
    second = float(candidates[1].get("score") or 0.0) if len(candidates) > 1 else 0.0
    return top < 0.34 or (top - second) < 0.08


def _catalog_route_by_intent(catalog: tuple[dict[str, Any], ...], intent_type: str) -> dict[str, Any] | None:
    clean = str(intent_type or "").strip()
    return next((item for item in catalog if item["intent_type"] == clean), None)


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
    args: dict[str, Any] | None = None,
) -> str:
    if not text.strip() or not route.get("intent_type") or not route.get("tool"):
        return ""
    request_args = args if isinstance(args, dict) else {}
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
                "state": str(request_args.get("state") or "").strip(),
                "diagnostic": bool(request_args.get("diagnostic", False)),
                "response_format": str(request_args.get("response_format") or "").strip(),
                "source_event_class": str(request_args.get("source_event_class") or request_args.get("event_class") or "").strip(),
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


TOOLS = [
    tool_definition("help"),
    tool_definition("state"),
    tool_definition("get"),
    tool_definition("submit"),
    tool_definition("put"),
    tool_definition("ask_project"),
    tool_definition("memory_store"),
    tool_definition("memory_search"),
    tool_definition("memory_tree_slice"),
    tool_definition("memory_context"),
    tool_definition("record_memory_outcome"),
    tool_definition("memory_recent"),
    tool_definition("memory_get"),
    tool_definition("memory_delete"),
    tool_definition("memory_batch_store"),
    tool_definition("memory_cleanup"),
    tool_definition("memory_stats"),
    tool_definition("registry_best"),
    tool_definition("registry_update"),
    tool_definition("registry_components"),
    tool_definition("crystallize_solution"),
    tool_definition("draft_skill"),
    tool_definition("route_task"),
    tool_definition("track_task"),
    tool_definition("tracker_stats"),
    tool_definition("report_issue"),
    tool_definition("load_instruction_layer"),
    tool_definition("list_instruction_layers"),
    tool_definition("list_project_laws"),
    tool_definition("list_learning_candidates"),
    tool_definition("approve_learning_candidate"),
    tool_definition("defer_learning_candidate"),
    tool_definition("reject_learning_candidate"),
    tool_definition("get_project_law"),
    tool_definition("project_rule_candidates_from_stenography"),
    tool_definition("project_rules"),
    tool_definition("project_context"),
    tool_definition("project_verify"),
    tool_definition("project_capture"),
    tool_definition("list_rule_candidates"),
    tool_definition("get_rule_candidate_review_packet"),
    tool_definition("review_rule_candidate"),
    tool_definition("promote_rule_candidate"),
    tool_definition("revise_law_from_rule_candidate"),
    tool_definition("improvements_report"),
    tool_definition("knowledge_hierarchy"),
    tool_definition("canonicals_by_scope"),
    tool_definition("set_canonical_status"),
    tool_definition("merge_canonicals"),
    tool_definition("skill_search"),
    tool_definition("skill_publish"),
    tool_definition("skill_install"),
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
    tool_definition("list_closeable_completed_tail"),
    tool_definition("reconcile_completed_checkpoints"),
    tool_definition("review_completed_checkpoint_scope"),
    tool_definition("review_completed_checkpoint_scopes"),
    tool_definition("resolve_artifact"),
    tool_definition("reopen_artifact"),
    tool_definition("memory_health"),
    tool_definition("system_info"),
    tool_definition("get_onboarding"),
    tool_definition("record_outcome"),
    tool_definition("ingest_file"),
    tool_definition("model_available"),
    tool_definition("report_limit_hit"),
    tool_definition("handoff_task"),
    tool_definition("pickup_handoff"),
    tool_definition("list_pending_handoff_labels"),
    tool_definition("list_handoffs"),
    tool_definition("handoff_workspace_summary"),
    tool_definition("decompose_task_packet"),
    tool_definition("create_task_packets"),
    tool_definition("route_task_packet_execution"),
    tool_definition("dispatch_background_task_packet"),
    tool_definition("reconcile_background_task_packet"),
    tool_definition("expand_handoff_refs"),
    tool_definition("refresh_handoff_context"),
    tool_definition("update_handoff_status"),
    tool_definition("resume_handoff"),
    tool_definition("ingest_dir"),
    tool_definition("search_project_knowledge"),
    tool_definition("enrich_task_with_context"),
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
    tool_definition("get_task_status"),
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
    "list_closeable_completed_tail",
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
