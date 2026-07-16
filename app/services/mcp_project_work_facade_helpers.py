"""Facade helper functions for project_work response shaping and recovery packets."""
from __future__ import annotations

from typing import Any

from app.services.data_hygiene_service import build_maintenance_suggestion
from app.services.mcp_response_filter import response_profile_from_args
from app.services.mcp_route_formatters import wants_route_diagnostic
from app.services.mcp_workflow_specs import load_named_json_spec
from app.services.server_build_info import server_build_diagnostics_enabled


def project_work_lease_conflict_recovery_packet(
    *,
    lease_guard: dict[str, Any],
    route: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    """Add an agent-facing FSM recovery recipe without weakening lease enforcement."""

    payload = route.get("payload") if isinstance(route.get("payload"), dict) else {}
    project = str(payload.get("project") or args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(payload.get("task_id") or args.get("task_id") or lease_guard.get("task_id") or "").strip()
    work_handle = str(payload.get("work_handle") or args.get("work_handle") or "").strip()
    recovery_spec = _project_work_recovery_spec()
    known_work_handle = work_handle or str(recovery_spec.get("known_work_handle_placeholder") or "").strip()
    latest_work_handle = str(recovery_spec.get("latest_active_work_handle_placeholder") or "").strip()
    render_context = {
        "project": project,
        "task_id": task_id,
        "known_work_handle": known_work_handle,
        "latest_active_work_handle": latest_work_handle,
    }
    recommended_next_call = _render_recovery_template(recovery_spec.get("recommended_next_call") or {}, render_context)
    recovery_steps = _render_recovery_template(recovery_spec.get("steps") or [], render_context)

    enriched = dict(lease_guard)
    enriched["next_safe_action"] = str(recovery_spec.get("next_safe_action") or "").strip()
    enriched["recommended_next_call"] = recommended_next_call
    enriched["recovery_protocol"] = {
        "name": str(recovery_spec.get("name") or "").strip(),
        "reason": str(recovery_spec.get("reason") or "").strip(),
        "preserves_lease_enforcement": bool(recovery_spec.get("preserves_lease_enforcement", True)),
        "requires_active_work_handle": bool(recovery_spec.get("requires_active_work_handle", True)),
        "handle_rule": str(recovery_spec.get("handle_rule") or "").strip(),
        "steps": recovery_steps,
    }
    return enriched


def redact_project_work_submit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public_payload = dict(payload)
    if public_payload.get("work_token"):
        public_payload["work_token"] = "[REDACTED]"
    return public_payload


def sanitize_project_work_result(result: Any, args: dict[str, Any]) -> Any:
    profile = response_profile_from_args(args)
    if wants_route_diagnostic(args) or server_build_diagnostics_enabled():
        profile = "diagnostic"
    return _sanitize_project_work_value(result, include_legacy_token=profile == "diagnostic")


def project_work_maintenance_suggestion(*, route: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if str(route.get("intent_type") or "") != "next_priority":
        return {}
    project = str((route.get("payload") or {}).get("project") or args.get("project") or "").strip()
    if not project:
        return {}
    try:
        suggestion = build_maintenance_suggestion(current_project=project)
    except Exception:
        return {}
    if str(suggestion.get("status") or "") != "warning":
        return {}
    return {
        key: value
        for key, value in suggestion.items()
        if key
        in {
            "status",
            "active_findings",
            "top_dataset_classes",
            "top_recommended_actions",
            "scope",
            "why_it_matters",
            "next_safe_action",
            "destructive_action_allowed",
            "expand_refs",
        }
        and value not in (None, "", [], {})
    }


def _project_work_recovery_spec() -> dict[str, Any]:
    spec = load_named_json_spec("workflow/project_work_recovery.json")
    value = spec.get("public_fsm_closeout_recovery")
    return value if isinstance(value, dict) else {}


def _render_recovery_template(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, replacement in context.items():
            rendered = rendered.replace("{" + key + "}", replacement)
        return rendered
    if isinstance(value, list):
        return [_render_recovery_template(item, context) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _render_recovery_template(item, context)
            for key, item in value.items()
        }
    return value


def _sanitize_project_work_value(value: Any, *, include_legacy_token: bool) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "work_token_hash":
                continue
            if key_text == "work_token" and not include_legacy_token:
                continue
            sanitized[key_text] = _sanitize_project_work_value(item, include_legacy_token=include_legacy_token)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_project_work_value(item, include_legacy_token=include_legacy_token) for item in value]
    return value
