from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.mcp_mailbox_read import (
    MailboxReadDependencies,
    build_mailbox_get_response,
    build_mailbox_state_response,
)
from app.services.context_cue_service import context_cues_for_query
from app.services.planning_advisor_service import task_framing_gaps_from_context
from app.services.mcp_user_explanation_service import user_explanation_for_artifact, user_explanation_for_task


SessionIdentityCallback = Callable[[str | None], Awaitable[dict[str, str]]]
GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
RefResolverCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
QueryResolverCallback = Callable[[str, dict[str, Any], str | None], Awaitable[dict[str, Any] | None]]
SubmitCallback = Callable[[dict[str, Any], dict[str, Any], str, str | None], Awaitable[dict[str, Any]]]
ToolSurfaceRoleCallback = Callable[[str], str]


@dataclass(frozen=True)
class SimpleSurfaceDependencies:
    get: GetCallback
    get_session_identity_defaults: SessionIdentityCallback
    resolve_public_ref: RefResolverCallback
    resolve_query: QueryResolverCallback
    submit_mailbox_form: SubmitCallback
    tool_surface_role: ToolSurfaceRoleCallback


def build_simple_help_response(args: dict[str, Any]) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    topic = str(args.get("topic") or "").strip()
    detail = str(args.get("detail") or "brief").strip().lower()
    if detail not in {"brief", "full"}:
        detail = "brief"
    guide: dict[str, Any] = {
        "status": "ok",
        "project": project,
        "purpose": "Use the four public tools as a stable mailbox protocol.",
        "tools": {
            "help": "Static protocol guide. Use this when you do not know what to call.",
            "state": "Current workflow/FSM packet with allowed public forms and next safe action.",
            "get": "Read data by public ref/address or ask a natural read-only question.",
            "submit": "Submit a public form/action payload; server applies guardrails before mutation.",
            "put": "Compatibility alias for submit.",
        },
        "examples": [
            {"tool": "state", "arguments": {"project": project, "state": "planning"}},
            {"tool": "get", "arguments": {"ref": f"task:{project}:<task_id>"}},
            {"tool": "get", "arguments": {"query": "list active tasks", "project": project}},
            {"tool": "submit", "arguments": {"action": "get_task_context", "payload": {"project": project, "task_id": "<task_id>"}}},
        ],
        "rules": [
            "Use get/state before submit when context is incomplete.",
            "Do not invent internal refs; use refs returned by get/state/results.",
            "Diagnostics are optional parameters and may be ignored for weak runtime profiles.",
        ],
        "topic": topic,
        "next_safe_action": "Call state for the current workflow packet, or get for a read-only request.",
    }
    if detail == "full":
        guide["legacy_compatibility"] = {
            "mailbox_state": "legacy alias for state",
            "mailbox_get": "legacy read-by-ref surface behind get",
            "mailbox_submit": "legacy form submit surface behind submit",
        }
    guide["simple_interface"] = {
        "tools": ["help", "state", "get", "submit"],
        "guide": "Call help for protocol guidance.",
        "state": "Call state for current forms/actions.",
        "read": "Call get with ref for a known public address, or query for a natural read-only question.",
        "write": "Call submit with form_id/action and payload from the state packet. put remains a compatibility alias.",
        "topic": topic,
    }
    return guide


async def build_simple_state_response(
    *,
    api_base: str,
    args: dict[str, Any],
    dependencies: SimpleSurfaceDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    scoped_args = args_with_session_project(args, identity_defaults)
    data = await build_mailbox_state_response(
        args={
            "project": str(scoped_args.get("project") or "mnemoforge"),
            "state": str(scoped_args.get("state") or "planning"),
            "runtime_profile_id": str(scoped_args.get("runtime_profile_id") or "unknown_cli"),
            "diagnostic": bool(scoped_args.get("diagnostic", False)),
            "detail": str(scoped_args.get("detail") or "compact"),
            "task_id": str(scoped_args.get("task_id") or ""),
            "work_handle": str(scoped_args.get("work_handle") or ""),
            "work_token": str(scoped_args.get("work_token") or ""),
        },
        session_id=session_id,
        api_base=api_base,
        dependencies=MailboxReadDependencies(
            get_session_identity_defaults=dependencies.get_session_identity_defaults,
            get=dependencies.get,
        ),
    )
    data["simple_interface"] = {
        "tools": ["help", "state", "get", "submit"],
        "help": "Call help for protocol guidance.",
        "read": "Call get with ref/query.",
        "write": "Call submit with form_id/action and payload from this state packet. put remains a compatibility alias.",
    }
    data["next_safe_action"] = data.get("next_safe_action") or "Use get for reads or submit with one of the listed forms."
    if not _full_detail_requested(args):
        data["forms"] = [compact_public_form(form) for form in (data.get("forms") or []) if isinstance(form, dict)]
        if isinstance(data.get("cue_packet"), dict):
            data.pop("context_cues", None)
            data.pop("health_nudge", None)
        data["details_available"] = True
    return data


async def build_simple_get_response(
    *,
    api_base: str,
    args: dict[str, Any],
    dependencies: SimpleSurfaceDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    scoped_args = args_with_session_project(args, identity_defaults)
    ref = str(scoped_args.get("ref") or scoped_args.get("address") or scoped_args.get("data_ref") or "").strip()
    if ref:
        get_args = {**scoped_args, "ref": ref}
        data = await dependencies.resolve_public_ref(api_base, get_args)
        if data is None:
            data = await build_mailbox_get_response(
                args=get_args,
                session_id=session_id,
                dependencies=MailboxReadDependencies(
                    get_session_identity_defaults=dependencies.get_session_identity_defaults,
                    get=dependencies.get,
                ),
            )
        data["simple_interface"] = {"tool": "get", "mode": "ref"}
        return compact_simple_get_packet(data, scoped_args, tool_surface_role=dependencies.tool_surface_role)

    query = str(scoped_args.get("query") or scoped_args.get("question") or scoped_args.get("intent") or "").strip()
    if query:
        data = await dependencies.resolve_query(api_base, scoped_args, session_id)
        if data is not None:
            cues = context_cues_for_query(query=query, project=str(scoped_args.get("project") or ""))
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            if cues and not data.get("context_cues") and not result.get("context_cues"):
                data["context_cues"] = cues
            if _context_response_requested(scoped_args) and not _full_detail_requested(scoped_args):
                return context_simple_get_packet(data, scoped_args, tool_surface_role=dependencies.tool_surface_role)
            return data

    data = await build_simple_state_response(args=scoped_args, dependencies=dependencies, session_id=session_id)
    data["receipt"] = {
        "status": "needs_input",
        "message": "get requires ref/address/data_ref or query/question.",
        "next_safe_action": "Call get with a public ref or natural read-only query.",
    }
    return data


async def build_simple_submit_response(
    *,
    api_base: str,
    args: dict[str, Any],
    dependencies: SimpleSurfaceDependencies,
    session_id: str | None = None,
    public_tool_name: str = "submit",
) -> dict[str, Any]:
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    args = args_with_session_project(args, identity_defaults)
    payload = dict(args.get("payload")) if isinstance(args.get("payload"), dict) else {}
    form_id = str(args.get("form_id") or args.get("action") or payload.get("form_id") or "").strip()
    if not form_id:
        return {
            "state": str(args.get("state") or "planning"),
            "project": str(args.get("project") or payload.get("project") or "mnemoforge"),
            "receipt": {
                "status": "needs_input",
                "message": f"{public_tool_name} requires form_id or action plus payload.",
                "next_safe_action": "Call state for available forms, then submit with form_id and payload.",
            },
            "simple_interface": {"tool": public_tool_name},
            "next_safe_action": "Call state for available forms, then submit with form_id and payload.",
        }
    if args.get("detail") and "detail" not in payload:
        payload["detail"] = args.get("detail")
    submit_args = {
        "form_id": form_id,
        "state": str(args.get("state") or "planning"),
        "project": str(args.get("project") or payload.get("project") or "mnemoforge"),
        "payload": payload,
        "runtime_profile_id": str(args.get("runtime_profile_id") or "unknown_cli"),
        "diagnostic": bool(args.get("diagnostic", False)),
    }
    data = await dependencies.submit_mailbox_form(submit_args, payload, api_base, session_id)
    data["simple_interface"] = {"tool": public_tool_name, "form_id": form_id}
    return compact_simple_submit_packet(
        data,
        args,
        tool_surface_role=dependencies.tool_surface_role,
    )


def args_with_session_project(args: dict[str, Any], identity_defaults: dict[str, str]) -> dict[str, Any]:
    if str(args.get("project") or args.get("project_id") or "").strip():
        return args
    project = str(
        identity_defaults.get("project")
        or identity_defaults.get("project_id")
        or identity_defaults.get("default_project")
        or ""
    ).strip()
    if not project:
        return args
    scoped = dict(args)
    scoped["project"] = project
    return scoped


def compact_public_form(form: dict[str, Any]) -> dict[str, Any]:
    return {
        "form_id": form.get("form_id"),
        "title": form.get("title"),
        "mode": form.get("mode"),
        "required_fields": form.get("required_fields") or [],
        "optional_fields": form.get("optional_fields") or [],
        "hint": form.get("hint"),
    }


def compact_simple_get_packet(
    data: dict[str, Any],
    args: dict[str, Any],
    *,
    tool_surface_role: ToolSurfaceRoleCallback,
) -> dict[str, Any]:
    if _full_detail_requested(args):
        return data
    if _context_response_requested(args):
        return context_simple_get_packet(data, args, tool_surface_role=tool_surface_role)
    receipt = data.get("receipt") if isinstance(data.get("receipt"), dict) else {}
    kind = str(receipt.get("resource_kind") or "").strip()
    compact = dict(data)
    compact["result"] = compact_resource_result(
        kind,
        data.get("result"),
        tool_surface_role=tool_surface_role,
        state=str(data.get("state") or args.get("state") or "planning"),
    )
    compact["details_available"] = True
    return compact


def context_simple_get_packet(
    data: dict[str, Any],
    args: dict[str, Any],
    *,
    tool_surface_role: ToolSurfaceRoleCallback,
) -> dict[str, Any]:
    receipt = data.get("receipt") if isinstance(data.get("receipt"), dict) else {}
    result = data.get("result")
    kind = resource_kind_from_receipt_or_result(receipt, result)
    project = str(data.get("project") or args.get("project") or receipt.get("project") or "").strip()
    status = str(receipt.get("status") or data.get("status") or "").strip()
    if kind == "planning_advisor":
        return _clean_context_packet(
            {
                "kind": "workflow_decision_guardrail",
                "project": project,
                "status": status or "needs_compact_or_full",
                "warning": "Context response_format is for auxiliary read-only retrieval, not next-work or workflow decisions.",
                "next_safe_action": "Repeat this get request with response_format=auto or detail=compact for the workflow packet.",
            }
        )
    packet: dict[str, Any] = {
        "kind": kind or "resource",
        "project": project,
        "status": status,
    }
    ref = str(receipt.get("data_ref") or "").strip()
    if ref:
        packet["ref"] = ref
    requested_ref = str(receipt.get("requested_ref") or args.get("ref") or "").strip()
    if requested_ref and requested_ref != ref:
        packet["requested_ref"] = requested_ref
    if kind == "artifact_list" and isinstance(result, dict):
        packet["artifact_type"] = receipt.get("artifact_type")
        packet["status_filter"] = receipt.get("status_filter")
        items = result.get("items") if isinstance(result.get("items"), list) else []
        packet["items"] = [_context_item(item, tool_surface_role=tool_surface_role) for item in items if isinstance(item, dict)]
        packet["count"] = len(packet["items"])
    elif kind == "project_aliases" and isinstance(result, dict):
        packet["canonical_project_id"] = result.get("project_id") or result.get("canonical_project_id")
        packet["aliases"] = result.get("aliases") or []
    elif isinstance(result, dict):
        compact = compact_resource_result(
            kind,
            result,
            tool_surface_role=tool_surface_role,
            state=str(data.get("state") or args.get("state") or "planning"),
        )
        if isinstance(compact, dict):
            packet.update(_context_fields_from_compact(kind, compact))
    elif result not in (None, "", []):
        packet["content"] = result
    warning = _critical_warning(data, receipt)
    if warning:
        packet["warning"] = warning
    if status and status != "accepted":
        packet["next_safe_action"] = receipt.get("next_safe_action") or data.get("next_safe_action")
    return _clean_context_packet(packet)


def _context_item(item: dict[str, Any], *, tool_surface_role: ToolSurfaceRoleCallback) -> dict[str, Any]:
    kind = str(item.get("type") or item.get("kind") or "artifact").strip()
    compact = compact_resource_result(kind, item, tool_surface_role=tool_surface_role)
    if not isinstance(compact, dict):
        compact = dict(item)
    packet = _context_fields_from_compact(kind, compact)
    if compact.get("artifact_key") and "ref" not in packet:
        packet["ref"] = compact.get("artifact_key")
    packet["kind"] = kind
    return _clean_context_packet(packet)


def _context_fields_from_compact(kind: str, compact: dict[str, Any]) -> dict[str, Any]:
    packet: dict[str, Any] = {}
    direct_keys = (
        "kind",
        "ref",
        "artifact_key",
        "id",
        "task_id",
        "title",
        "status",
        "task_status",
        "description",
        "content",
        "statement",
        "summary",
        "memory_type",
        "category",
        "project",
        "user_explanation",
        "linked_artifact_key",
        "linked_status",
        "page_ref",
        "page_id",
        "parent_ref",
        "page_kind",
        "page_index",
        "version",
        "superseded_by_page_id",
    )
    for key in direct_keys:
        if compact.get(key) not in (None, "", []):
            packet[key] = compact.get(key)
    if kind == "task":
        latest = compact.get("latest_checkpoint") if isinstance(compact.get("latest_checkpoint"), dict) else {}
        if latest.get("summary") and "summary" not in packet:
            packet["summary"] = latest.get("summary")
        if latest.get("next_step"):
            packet["checkpoint_next_step"] = latest.get("next_step")
        gaps = compact.get("task_framing_gaps")
        if gaps:
            packet["warning"] = "Task framing has gaps: " + ", ".join(str(item) for item in gaps[:5])
        readiness = compact.get("execution_readiness") if isinstance(compact.get("execution_readiness"), dict) else {}
        if readiness.get("status") and readiness.get("status") != "ready":
            packet["readiness"] = readiness.get("status")
            if readiness.get("recommended_next_action"):
                packet["next_safe_action"] = readiness.get("recommended_next_action")
    if "page_ref" in packet and "ref" not in packet:
        packet["ref"] = packet["page_ref"]
    if "artifact_key" in packet and "ref" not in packet:
        packet["ref"] = packet["artifact_key"]
    return packet


def _critical_warning(data: dict[str, Any], receipt: dict[str, Any]) -> str:
    incident = receipt.get("diagnostic_incident") if isinstance(receipt.get("diagnostic_incident"), dict) else {}
    if incident.get("summary"):
        return str(incident.get("summary"))
    message = str(receipt.get("message") or "").strip()
    status = str(receipt.get("status") or "").strip()
    if status and status not in {"accepted", "ok"} and message:
        return message
    if data.get("warning"):
        return str(data.get("warning"))
    return ""


def _clean_context_packet(packet: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in packet.items() if value not in (None, "", [])}


def compact_simple_submit_packet(
    data: dict[str, Any],
    args: dict[str, Any],
    *,
    tool_surface_role: ToolSurfaceRoleCallback,
) -> dict[str, Any]:
    if _full_detail_requested(args):
        return data
    compact = dict(data)
    receipt = data.get("receipt") if isinstance(data.get("receipt"), dict) else {}
    if receipt:
        receipt_keys = (
            "status", "form_id", "mode", "message", "id", "artifact_key", "task_id",
            "canonical_task_id", "task_artifact_key", "linked_artifact_key", "stage", "data_ref", "approved_command",
            "lifecycle_stage", "implementation_ready", "claim_allowed", "framing_required",
            "authority_layer", "classification_reason", "matched_law_ref", "matched_law_title", "matched_law_status",
            "canonical_status", "created_task", "idempotent_reuse", "suppress_improvement",
            "evidence_classification",
            "forbidden_patterns", "work_handle", "work_token", "lease", "work_session",
            "work_session_resumed", "reclaim", "recommended_reclaim_call",
            "diagnostic_incident",
            "same_fingerprint", "recovery_options", "edit_authority", "autonomous_mode", "work_guidance",
            "release", "continuity_reclaim", "continuity_lease", "next_state", "next_forms", "close_status", "task_status",
            "linked_improvement_sync", "superseded_by", "submitted_fields", "next_safe_action",
            "requested_close_status", "recommended_next_call",
            "pattern_id", "feedback_action", "facade", "vote",
            "target_ref", "target_type", "refinement_type", "refinement_status", "mutation_executed",
            "postcondition_satisfied",
            "page_ref", "page_id", "parent_ref", "page_kind", "page_index", "version",
            "decision_id", "decision", "implemented_task_ref",
        )
        compact["receipt"] = {key: receipt.get(key) for key in receipt_keys if receipt.get(key) not in (None, "", [])}
    if "result" in compact:
        compact["result"] = compact_submit_result(
            compact.get("result"),
            receipt=receipt,
            tool_surface_role=tool_surface_role,
        )
    compact["details_available"] = True
    return compact


def compact_submit_result(
    result: Any,
    *,
    receipt: dict[str, Any],
    tool_surface_role: ToolSurfaceRoleCallback,
) -> Any:
    if not isinstance(result, dict):
        return result
    kind = resource_kind_from_receipt_or_result(receipt, result)
    return compact_resource_result(kind, result, tool_surface_role=tool_surface_role) if kind else result


def resource_kind_from_receipt_or_result(receipt: dict[str, Any], result: Any) -> str:
    kind = str(receipt.get("resource_kind") or "").strip()
    if kind:
        return kind
    data_ref = str(receipt.get("data_ref") or "").strip()
    for prefix in ("task", "improvement", "law", "rule_candidate", "memory", "cue"):
        if data_ref.startswith(f"{prefix}:"):
            return prefix
    if isinstance(result, dict):
        if result.get("task_id") and (isinstance(result.get("task"), dict) or isinstance(result.get("latest_checkpoint"), dict)):
            return "task"
        if result.get("artifact_key"):
            return str(result.get("type") or "artifact")
        if result.get("candidate_id"):
            return "rule_candidate"
    return ""


def compact_resource_result(
    kind: str,
    result: Any,
    *,
    tool_surface_role: ToolSurfaceRoleCallback,
    state: str = "planning",
) -> Any:
    if not isinstance(result, dict):
        return result
    if kind == "task":
        return {
            key: value
            for key, value in compact_task_resource(result, tool_surface_role=tool_surface_role, state=state).items()
            if value not in (None, "", [])
        }
    fields_by_kind = {
        "artifact": ("artifact_key", "type", "id", "project", "title", "description", "status", "stage", "linked_artifact_key", "linked_status"),
        "improvement": ("artifact_key", "type", "id", "project", "title", "description", "status", "stage", "linked_artifact_key", "linked_status"),
        "law": ("id", "project", "title", "status", "scope", "statement", "rationale", "version"),
        "rule_candidate": ("candidate_id", "project", "status", "scope", "statement", "rationale", "trial_review_after", "trial_expires_at"),
        "memory": ("id", "content", "memory_type", "category", "project", "created_at", "updated_at", "importance_score"),
        "cue": ("ref", "cue", "project", "severity", "scope", "title", "summary", "full_text", "source"),
        "context_page": ("page_ref", "page_id", "parent_ref", "project", "page_kind", "page_index", "title", "summary", "content", "version", "status", "superseded_by_page_id"),
    }
    fields = fields_by_kind.get(kind)
    if not fields:
        return result
    compact = {key: result.get(key) for key in fields if result.get(key) not in (None, "", [])}
    if kind in {"artifact", "improvement"}:
        compact["user_explanation"] = user_explanation_for_artifact(result, kind=kind)
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def compact_task_resource(result: dict[str, Any], *, tool_surface_role: ToolSurfaceRoleCallback, state: str = "planning") -> dict[str, Any]:
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    latest = result.get("latest_checkpoint") if isinstance(result.get("latest_checkpoint"), dict) else {}
    readiness = result.get("execution_readiness") if isinstance(result.get("execution_readiness"), dict) else {}
    replay = result.get("replay_completeness") if isinstance(result.get("replay_completeness"), dict) else {}
    drill = result.get("replay_drill") if isinstance(result.get("replay_drill"), dict) else {}
    task_id = result.get("task_id") or task.get("task_id")
    project = str(result.get("project") or "mnemoforge").strip() or "mnemoforge"
    is_done = str(result.get("status") or "").strip().lower() == "done" or str(task.get("status") or "").strip().lower() == "done"
    recommendation = public_recommendation_for_tool(
        result.get("recommended_first_tool"),
        result,
        tool_surface_role=tool_surface_role,
    )
    compact = {
        "agent_envelope": {
            "profile": "compact_task_context",
            "details_available": True,
            "detail_next_call": _task_detail_next_call(project=project, task_id=str(task_id or "")),
            "omitted_detail_refs": _omitted_task_context_refs(result),
        },
        "user_explanation": user_explanation_for_task(result, state=state),
        "task_id": task_id,
        "status": result.get("status"),
        "title": task.get("title") or result.get("title"),
        "task_status": task.get("status"),
        "task_framing_gaps": [] if is_done else task_framing_gaps_from_context(result, state=state),
        "latest_checkpoint": {
            "id": latest.get("id"),
            "stage": latest.get("stage"),
            "status": latest.get("status"),
            "summary": latest.get("summary"),
            "next_step": latest.get("next_step"),
        } if latest else None,
        "execution_readiness": {
            "status": readiness.get("status"),
            "missing_evidence": readiness.get("missing_evidence") or [],
            "recommended_next_action": readiness.get("recommended_next_action"),
        } if readiness else None,
        "replay_completeness": {
            "status": replay.get("status"),
            "missing_fields": replay.get("missing_fields") or [],
            "can_continue_without_user": replay.get("can_continue_without_user"),
        } if replay else None,
        "recovery_context": {
            "status": drill.get("status"),
            "first_tool": drill.get("first_tool"),
            "first_action": drill.get("first_action"),
        } if drill else None,
        "continuity": _task_continuity_summary(result),
        "safety_summary": _task_safety_summary(result),
        "token_footprint": _task_token_footprint(result),
        "stenography_coverage": result.get("stenography_coverage"),
        "recommended_first_tool": recommendation.get("tool"),
        "recommended_next_call": recommendation,
        "next_safe_action": result.get("next_safe_action"),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _task_detail_next_call(*, project: str, task_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"project": project, "detail": "full"}
    if task_id:
        payload["task_id"] = task_id
    return {
        "tool": "submit",
        "form_id": "get_task_context",
        "payload": payload,
        "why": "Request full task context only when compact fields or expand refs are insufficient.",
    }


def _omitted_task_context_refs(result: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in (
        "replay_bundle",
        "stenography_protocol",
        "project_identity",
        "available_layers",
        "resume_handoffs",
        "next_actions",
        "context_cues",
        "context_pages",
    ):
        value = result.get(key)
        if value not in (None, "", [], {}):
            refs.append(f"result.{key}")
    return refs


def _task_token_footprint(result: dict[str, Any]) -> dict[str, Any]:
    budget = result.get("token_budget") if isinstance(result.get("token_budget"), dict) else {}
    if not budget:
        return {}
    return {
        key: budget.get(key)
        for key in (
            "estimated_tokens",
            "budget_tokens",
            "within_budget",
            "overflow_tokens",
            "overflow_reason",
        )
        if budget.get(key) not in (None, "", [], {})
    }


def _compact_mapping(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value.get(key) for key in fields if value.get(key) not in (None, "", [], {})}


def _task_continuity_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary = _compact_mapping(
        result,
        (
            "work_handle",
            "work_id",
            "claim_status",
            "claim_allowed",
            "same_fingerprint_reclaim",
            "work_session_resumed",
        ),
    )
    lease = _compact_mapping(
        result.get("lease") or result.get("continuity_lease") or result.get("occupied_by"),
        ("lease_id", "status", "owner_agent", "owner_session_id", "session_id", "expires_at"),
    )
    if lease:
        summary["lease"] = lease
    work_session = _compact_mapping(result.get("work_session"), ("work_id", "status", "stage"))
    if work_session:
        summary["work_session"] = work_session
    reclaim = _compact_mapping(result.get("continuity_reclaim"), ("status", "lease_id", "reason"))
    if reclaim:
        summary["continuity_reclaim"] = reclaim
    recommended_reclaim = _compact_mapping(result.get("recommended_reclaim_call"), ("tool", "form_id", "why"))
    if recommended_reclaim:
        summary["recommended_reclaim_call"] = recommended_reclaim
    return summary


def _task_safety_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    edit_authority = _compact_mapping(
        result.get("edit_authority"),
        ("status", "severity", "editing_allowed", "authority_source", "reason", "next_safe_action"),
    )
    if edit_authority:
        summary["edit_authority"] = edit_authority
    weak_guardrail = _compact_mapping(
        result.get("weak_model_guardrail"),
        ("mutation_executed", "confirmation_required", "do_not_claim_created", "plain_instruction"),
    )
    if weak_guardrail:
        summary["weak_model_guardrail"] = weak_guardrail
    recovery_protocol = _compact_mapping(
        result.get("recovery_protocol"),
        ("status", "next_tool", "requires_active_work_handle", "known_work_handle", "latest_active_work_handle"),
    )
    if recovery_protocol:
        summary["recovery_protocol"] = recovery_protocol
    return summary
def public_recommendation_for_tool(
    tool_name: Any,
    result: dict[str, Any],
    *,
    tool_surface_role: ToolSurfaceRoleCallback,
) -> dict[str, Any]:
    internal_tool = str(tool_name or "").strip()
    project = str(result.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(result.get("task_id") or "").strip()
    if internal_tool in {"record_task_checkpoint", "report_task_checkpoint"}:
        return _form_recommendation("record_progress", project, task_id, internal_tool, "Use the public form-submission surface instead of calling checkpoint tools directly.")
    if internal_tool in {"pull_task_context", "get_task_execution_context"}:
        return _form_recommendation("get_task_context", project, task_id, internal_tool, "Use the public form-submission surface for task context.")
    if internal_tool:
        is_public = tool_surface_role(internal_tool) == "public_entrypoint"
        return {
            "tool": internal_tool if is_public else "state",
            "why": "Request the current workflow state before using specialized fallback tools.",
            "internal_tool": "" if is_public else internal_tool,
        }
    return {"tool": "state", "why": "Request the current workflow state before choosing the next action."}


def _form_recommendation(form_id: str, project: str, task_id: str, internal_tool: str, why: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"project": project}
    if task_id:
        payload["task_id"] = task_id
    return {
        "tool": "submit",
        "form_id": form_id,
        "payload": payload,
        "why": why,
        "internal_tool": internal_tool,
    }


def _context_response_requested(args: dict[str, Any]) -> bool:
    return str(args.get("response_format") or "").strip().lower() == "context"


def _full_detail_requested(args: dict[str, Any]) -> bool:
    return str(args.get("detail") or "compact").strip().lower() == "full" or bool(args.get("diagnostic", False))
