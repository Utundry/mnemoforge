from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.operational_instincts_service import build_operational_instinct_playbook


PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
FormatterCallback = Callable[..., str]
PayloadFormatterCallback = Callable[[dict[str, Any]], str]
HandoffRefsCallback = Callable[[dict[str, Any]], dict[str, list[str]]]
HandoffSummaryCallback = Callable[[dict[str, Any]], str]
HandoffExtractCallback = Callable[[dict[str, Any], str], Any]
ContentPreviewCallback = Callable[[str], str]


@dataclass(frozen=True)
class HandoffActionDependencies:
    post: PostCallback
    get: GetCallback
    build_handoff_context_summary: HandoffSummaryCallback
    build_handoff_context_refs: HandoffRefsCallback
    summarize_ref_counts: PayloadFormatterCallback
    format_scope: FormatterCallback
    format_background_payload: FormatterCallback
    extract_handoff_field: HandoffExtractCallback
    sanitize_content_preview: ContentPreviewCallback
    format_workspace_summary: PayloadFormatterCallback
    format_decomposition: PayloadFormatterCallback
    format_created_task_packets: PayloadFormatterCallback
    format_route_task_packet_execution: PayloadFormatterCallback
    format_dispatch_background_task_packet: PayloadFormatterCallback
    format_reconcile_background_task_packet: PayloadFormatterCallback


HANDOFF_ACTIONS = {
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
}


async def execute_handoff_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
    if name == "handoff_task":
        return await _handoff_task(args=args, api_base=api_base, dependencies=dependencies)

    if name == "pickup_handoff":
        return await _pickup_handoff(args=args, api_base=api_base, dependencies=dependencies)

    if name == "list_pending_handoff_labels":
        qs = f"/models/handoff/pending_labels?agent_id={quote(str(args['agent_id']))}&limit={int(args.get('limit', 20))}"
        data = await dependencies.get(api_base, qs)
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

    if name == "list_handoffs":
        return await _list_handoffs(args=args, api_base=api_base, dependencies=dependencies)

    if name == "handoff_workspace_summary":
        data = await dependencies.post(api_base, "/models/handoff/workspace_summary", args)
        return dependencies.format_workspace_summary(data)

    if name == "decompose_task_packet":
        data = await dependencies.post(api_base, "/models/handoff/decompose", args)
        return dependencies.format_decomposition(data)

    if name == "create_task_packets":
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
        data = await dependencies.post(api_base, "/models/handoff/create_packets", payload)
        return dependencies.format_created_task_packets(data)

    if name == "route_task_packet_execution":
        payload = dict(args)
        if isinstance(payload.get("packet"), str):
            payload["packet"] = json.loads(payload["packet"])
        data = await dependencies.post(api_base, "/models/handoff/route_execution", payload)
        return dependencies.format_route_task_packet_execution(data)

    if name == "dispatch_background_task_packet":
        data = await dependencies.post(api_base, "/models/handoff/dispatch_background", args)
        return dependencies.format_dispatch_background_task_packet(data)

    if name == "reconcile_background_task_packet":
        data = await dependencies.post(api_base, "/models/handoff/reconcile_background", args)
        return dependencies.format_reconcile_background_task_packet(data)

    if name == "expand_handoff_refs":
        return await _expand_handoff_refs(args=args, api_base=api_base, dependencies=dependencies)

    if name == "refresh_handoff_context":
        return await _refresh_handoff_context(args=args, api_base=api_base, dependencies=dependencies)

    if name == "update_handoff_status":
        return await _update_handoff_status(args=args, api_base=api_base, dependencies=dependencies)

    if name == "resume_handoff":
        return await _resume_handoff(args=args, api_base=api_base, dependencies=dependencies)

    raise ValueError(f"Unsupported handoff action: {name}")


async def _handoff_task(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
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
            enrich_data = await dependencies.post(
                api_base,
                "/project/enrich-task",
                {
                    "project_id": project_id,
                    "task": args["task_description"],
                    "max_components": int(args.get("context_max_components", 3)),
                    "context_profile": "handoff_compact",
                },
            )
            project_context_summary = dependencies.build_handoff_context_summary(enrich_data) or None
            project_context_refs = dependencies.build_handoff_context_refs(enrich_data)
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
    data = await dependencies.post(api_base, "/models/handoff", payload)
    next_models = ", ".join(model["model_id"] for model in data.get("next_available", []))
    label_line = f"handoff_label: {data['handoff_label']}\n" if data.get("handoff_label") else ""
    phase_line = f"phase: {data['phase']}\n" if data.get("phase") else ""
    priority_line = f"priority: {data['priority']}\n" if data.get("priority") else ""
    owner_agent = data.get("owner_agent") or args.get("owner_agent")
    owner_agent_line = f"owner_agent: {owner_agent}\n" if owner_agent else ""
    write_scope = data.get("write_scope") or args.get("write_scope") or []
    write_scope_line = f"write_scope: {dependencies.format_scope(write_scope)}\n" if write_scope else ""
    core_line = f"core_instinct_ids: {', '.join(data.get('core_instinct_ids') or [])}\n" if data.get("core_instinct_ids") else ""
    objective_line = f"phase_objective: {data['phase_objective']}\n" if data.get("phase_objective") else ""
    execution_mode = data.get("execution_mode") or args.get("execution_mode")
    execution_mode_line = f"execution_mode: {execution_mode}\n" if execution_mode else ""
    background_job_type = data.get("background_job_type") or args.get("background_job_type")
    background_job_type_line = f"background_job_type: {background_job_type}\n" if background_job_type else ""
    background_payload = data.get("background_payload") or args.get("background_payload") or {}
    background_payload_line = f"background_payload: {dependencies.format_background_payload(background_payload)}\n" if background_payload else ""
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


async def _pickup_handoff(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
    data = await dependencies.post(api_base, "/models/handoff/pickup", args)
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
    for index, handoff in enumerate(data["handoffs"], 1):
        lines.append(f"\n--- Handoff {index} ---")
        lines.append(f"task_id: {handoff['task_id']}")
        if handoff.get("handoff_label"):
            lines.append(f"handoff_label: {handoff['handoff_label']}")
        lines.append(f"from: {handoff['from_agent']}")
        lines.append(f"memory_id: {handoff['memory_id']}")
        _append_handoff_detail_lines(lines, handoff, dependencies=dependencies)
        content_preview = dependencies.sanitize_content_preview(handoff.get("content") or "")
        if content_preview.strip():
            lines.append(content_preview)
    return "\n".join(lines)


async def _list_handoffs(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
    data = await dependencies.post(api_base, "/models/handoff/list", args)
    if data["found"] == 0:
        requested = ", ".join(data.get("statuses") or ["all"])
        return f"No handoffs for agent '{args['agent_id']}' matched statuses '{requested}'."
    lines = [f"Handoffs for '{args['agent_id']}' ({', '.join(data.get('statuses') or ['all'])}):"]
    for item in data["handoffs"]:
        label = item.get("handoff_label") or "-"
        phase = item.get("phase") or "-"
        priority = item.get("priority") or "-"
        owner_agent = dependencies.extract_handoff_field(item, "owner_agent")
        write_scope = dependencies.extract_handoff_field(item, "write_scope")
        result_summary = dependencies.extract_handoff_field(item, "result_summary")
        executor_used = dependencies.extract_handoff_field(item, "executor_used")
        model_used = dependencies.extract_handoff_field(item, "model_used")
        execution_mode = item.get("execution_mode")
        background_job_type = item.get("background_job_type")
        background_payload = item.get("background_payload")
        background_job_status = item.get("background_job_status")
        dispatched_job_id = item.get("dispatched_job_id")
        lines.append(
            f"- {item.get('task_id') or 'unknown'} label={label} status={item.get('status') or 'unknown'} "
            f"phase={phase} priority={priority} memory_id={item.get('memory_id')}"
            + (f" owner_agent={owner_agent}" if owner_agent else "")
            + (f" write_scope={dependencies.format_scope(write_scope)}" if write_scope else "")
            + (f" execution_mode={execution_mode}" if execution_mode else "")
            + (f" background_job_type={background_job_type}" if background_job_type else "")
            + (f" background_payload={dependencies.format_background_payload(background_payload)}" if background_payload else "")
            + (f" background_job_status={background_job_status}" if background_job_status else "")
            + (f" dispatched_job_id={dispatched_job_id}" if dispatched_job_id else "")
            + (f" executor_used={executor_used}" if executor_used else "")
            + (f" model_used={model_used}" if model_used else "")
            + (f" result_summary={result_summary}" if result_summary else "")
        )
    return "\n".join(lines)


async def _expand_handoff_refs(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
    data = await dependencies.post(api_base, "/models/handoff/expand_refs", args)
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
            lines.append(_format_expanded_ref_item(ref_type, item))
    unresolved = data.get("unresolved") or {}
    if unresolved:
        lines.append(
            "unresolved: " + ", ".join(f"{key}={len(values)}" for key, values in unresolved.items() if values)
        )
    return "\n".join(lines)


def _format_expanded_ref_item(ref_type: str, item: dict[str, Any]) -> str:
    if ref_type == "laws":
        return f"- {item.get('id')} [{item.get('status')}] {item.get('title')}: {str(item.get('statement') or '')[:140]}"
    if ref_type == "components":
        return f"- {item.get('component_id')} {item.get('name')}: {str(item.get('summary') or '')[:140]}"
    if ref_type == "improvements":
        return f"- {item.get('id')} [{item.get('status')}] {item.get('title')}: {str(item.get('description') or '')[:140]}"
    if ref_type == "runtime_hints":
        return f"- {item.get('id')} [{item.get('status')}] {item.get('action_type')}: {str(item.get('content') or '')[:140]}"
    if ref_type == "tasks":
        return f"- {item.get('task_id')} [{item.get('status')}] {item.get('title')}: {str(item.get('description') or '')[:140]}"
    if ref_type == "task_capture_candidates":
        return f"- {item.get('artifact_id')} [{item.get('status')}] {item.get('kind')} for {item.get('task_id') or 'unknown-task'}: {str(item.get('content') or '')[:140]}"
    if ref_type == "docs_sections":
        return f"- {item.get('section_key')} {item.get('name')}: {str(item.get('content_preview') or '')[:140]}"
    return f"- {item}"


async def _refresh_handoff_context(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
    data = await dependencies.post(api_base, "/models/handoff/refresh_context", args)
    lines = [f"Refreshed handoff context for {data['memory_id']}"]
    for key, label in (
        ("status", "status"),
        ("project_id", "project_id"),
        ("owner_agent", "owner_agent"),
    ):
        if data.get(key):
            lines.append(f"{label}: {data[key]}")
    if data.get("write_scope"):
        lines.append(f"write_scope: {dependencies.format_scope(data['write_scope'])}")
    if data.get("task_description"):
        lines.append(f"task: {data['task_description']}")
    if data.get("project_context_summary"):
        lines.append(f"project_context_summary: {data['project_context_summary']}")
    refs = data.get("project_context_refs") or {}
    if refs:
        lines.append("project_context_refs: " + dependencies.summarize_ref_counts(refs))
    coverage = data.get("coverage") or {}
    if coverage:
        lines.append("coverage: " + ", ".join(f"{key}={value}" for key, value in coverage.items() if value))
    if data.get("code_inspection_recommended"):
        lines.append("code_inspection_recommended: true")
    return "\n".join(lines)


async def _update_handoff_status(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
    data = await dependencies.post(api_base, "/models/handoff/status", args)
    lines = [f"Updated handoff status for {data['memory_id']}", f"status: {data['status']}"]
    _append_status_tail(lines, data, dependencies=dependencies)
    return "\n".join(lines)


async def _resume_handoff(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: HandoffActionDependencies,
) -> str:
    data = await dependencies.post(api_base, "/models/handoff/resume", args)
    lines = [f"Resumed handoff {data['memory_id']}", f"status: {data['status']}"]
    lines.append(f"refreshed: {'true' if data.get('refreshed') else 'false'}")
    _append_status_tail(lines, data, dependencies=dependencies)
    for key, label in (
        ("project_id", "project_id"),
        ("phase", "phase"),
        ("priority", "priority"),
        ("task_description", "task"),
        ("phase_objective", "phase_objective"),
        ("definition_of_done", "definition_of_done"),
        ("expected_output_shape", "expected_output_shape"),
        ("project_context_summary", "project_context_summary"),
    ):
        if data.get(key):
            lines.append(f"{label}: {data[key]}")
    refs = data.get("project_context_refs") or {}
    if refs:
        lines.append("project_context_refs: " + dependencies.summarize_ref_counts(refs))
    return "\n".join(lines)


def _append_status_tail(lines: list[str], data: dict[str, Any], *, dependencies: HandoffActionDependencies) -> None:
    for key in ("owner_agent", "executor_used", "model_used", "result_summary", "verification_summary", "acted_by", "reason"):
        if data.get(key):
            lines.append(f"{key}: {data[key]}")
    if data.get("write_scope"):
        lines.insert(2, f"write_scope: {dependencies.format_scope(data['write_scope'])}")


def _append_handoff_detail_lines(lines: list[str], handoff: dict[str, Any], *, dependencies: HandoffActionDependencies) -> None:
    for key in (
        "status",
        "project_id",
        "phase",
        "priority",
        "execution_mode",
        "background_job_type",
        "background_job_status",
        "dispatched_job_id",
        "definition_of_done",
        "expected_output_shape",
        "phase_objective",
        "project_context_summary",
    ):
        if handoff.get(key):
            lines.append(f"{key}: {handoff[key]}")
    owner_agent = dependencies.extract_handoff_field(handoff, "owner_agent")
    if owner_agent:
        lines.append(f"owner_agent: {owner_agent}")
    write_scope = dependencies.extract_handoff_field(handoff, "write_scope")
    if write_scope:
        lines.append(f"write_scope: {dependencies.format_scope(write_scope)}")
    if handoff.get("background_payload"):
        lines.append(f"background_payload: {dependencies.format_background_payload(handoff['background_payload'])}")
    if handoff.get("core_instinct_ids"):
        lines.append(f"core_instinct_ids: {', '.join(handoff['core_instinct_ids'])}")
    if handoff.get("supporting_instinct_ids"):
        lines.append(f"supporting_instinct_ids: {', '.join(handoff['supporting_instinct_ids'])}")
    if handoff.get("project_context_refs"):
        lines.append("project_context_refs: " + dependencies.summarize_ref_counts(handoff.get("project_context_refs") or {}))
        lines.append(f"Use expand_handoff_refs(memory_id='{handoff['memory_id']}') to inspect referenced context.")
    if handoff.get("project_context_snapshot"):
        lines.append("project_context_snapshot:")
        lines.append(handoff["project_context_snapshot"][:1200])
