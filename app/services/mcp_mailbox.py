from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.mcp_workflow import MailboxFormSpec, RuntimeProfilePreset, WorkflowStateName
from app.services.mcp_workflow_specs import (
    DEFAULT_SPEC_ROOT,
    list_mailbox_forms_for_state,
    load_feature_toggle_registry,
    load_mailbox_form_policy_spec,
    load_mailbox_protocol_spec,
    load_response_envelope_spec,
    load_runtime_profile_spec,
    load_state_spec,
    list_mailbox_form_specs,
)

_WORKFLOW_STATE_NAMES = {
    "planning",
    "implementation",
    "verification",
    "live_validation",
    "checkpointing",
    "handoff",
    "operator_review",
}


def _runtime_profile(profile_id: str, *, spec_root: Path) -> RuntimeProfilePreset:
    runtime_spec = load_runtime_profile_spec(spec_root=spec_root)
    profiles = {profile.id: profile for profile in runtime_spec.profile_presets}
    return profiles.get(str(profile_id or "").strip()) or profiles.get("unknown_cli") or runtime_spec.profile_presets[0]


def _public_form_payload(form: MailboxFormSpec) -> dict[str, Any]:
    return {
        "form_id": form.id,
        "title": form.title,
        "purpose": form.purpose,
        "mode": form.mode,
        "required_fields": form.required_fields,
        "optional_fields": form.optional_fields,
        "input_schema": form.input_schema,
        "assistance": form.assistance.model_dump(mode="json"),
        "hint": form.public_hint,
    }


def _internal_form_payload(form: MailboxFormSpec) -> dict[str, Any]:
    return {
        "form_id": form.id,
        "feature_toggles": form.feature_toggles,
        "replacement_form_ids": form.replacement_form_ids,
        "postconditions": form.postconditions.model_dump(mode="json"),
    }


def mailbox_form_state_names(form: MailboxFormSpec) -> set[str]:
    return {str(item.value if hasattr(item, "value") else item) for item in form.states}


def mailbox_form_by_id(form_id: str, *, spec_root: Path = DEFAULT_SPEC_ROOT) -> MailboxFormSpec | None:
    normalized = str(form_id or "").strip()
    return next((form for form in list_mailbox_form_specs(spec_root=spec_root) if form.id == normalized), None)


def build_mailbox_state_packet(
    *,
    state: WorkflowStateName | str,
    project: str = "mnemoforge",
    runtime_profile_id: str = "unknown_cli",
    diagnostic: bool = False,
    detail: str = "compact",
    spec_root: Path = DEFAULT_SPEC_ROOT,
) -> dict[str, Any]:
    state_spec = load_state_spec(state, spec_root=spec_root)
    runtime_profile = _runtime_profile(runtime_profile_id, spec_root=spec_root)
    all_forms = list_mailbox_forms_for_state(state_spec.id, spec_root=spec_root)
    forms, hidden_form_ids = _public_forms_for_runtime(
        state=state_spec.id,
        forms=all_forms,
        runtime_profile=runtime_profile,
        spec_root=spec_root,
    )
    feature_registry = load_feature_toggle_registry(spec_root=spec_root)
    envelope = load_response_envelope_spec(spec_root=spec_root)
    protocol = load_mailbox_protocol_spec(spec_root=spec_root)

    disabled_features = _disabled_feature_ids(
        runtime_profile_id=runtime_profile.id,
        project=project,
        runtime_disabled=set(runtime_profile.default_disabled_features),
        spec_root=spec_root,
    )
    affected_forms = [
        form.id
        for form in forms
        if disabled_features.intersection(form.feature_toggles)
    ]
    warnings: list[str] = []
    if affected_forms:
        warnings.append(
            "Some internal routes are disabled for this runtime profile; the server must use safe replacement routes."
        )
    if diagnostic and not runtime_profile.allow_internal_diagnostics:
        warnings.append("Internal diagnostics are not available for this runtime profile.")

    public_packet: dict[str, Any] = {
        "state": state_spec.id,
        "project": project,
        "instruction": state_spec.purpose,
        "forms": [_public_form_payload(form) for form in forms],
        "hidden_forms": hidden_form_ids,
        "warnings": warnings,
        "next_safe_action": _next_safe_action(state_spec.id, forms),
        "receipt": None,
    }
    _apply_packet_limit(
        public_packet,
        forms=forms,
        hidden_form_ids=hidden_form_ids,
        packet_profile=runtime_profile.packet_profile,
        detail=detail,
        spec_root=spec_root,
    )

    if diagnostic and runtime_profile.allow_internal_diagnostics:
        toggles_by_id = {toggle.id: toggle for toggle in feature_registry.toggles}
        public_packet["_internal"] = {
            "visibility": "internal",
            "runtime_profile": runtime_profile.model_dump(mode="json"),
            "mailbox_protocol": protocol.model_dump(mode="json"),
            "response_envelope": envelope.model_dump(mode="json"),
            "state_spec": state_spec.model_dump(mode="json"),
            "disabled_features": sorted(disabled_features),
            "disabled_feature_details": [
                toggles_by_id[feature_id].model_dump(mode="json")
                for feature_id in sorted(disabled_features)
                if feature_id in toggles_by_id
            ],
            "affected_forms": affected_forms,
            "forms": [_internal_form_payload(form) for form in forms],
            "hidden_forms": [
                _internal_form_payload(form)
                for form in all_forms
                if form.id in hidden_form_ids
            ],
        }

    return public_packet


def _public_forms_for_runtime(
    *,
    state: str,
    forms: list[MailboxFormSpec],
    runtime_profile: RuntimeProfilePreset,
    spec_root: Path,
) -> tuple[list[MailboxFormSpec], list[str]]:
    form_policy = load_mailbox_form_policy_spec(spec_root=spec_root)
    priority = form_policy.state_priorities.get(state, [])
    rank = {form_id: index for index, form_id in enumerate(priority)}
    sorted_forms = sorted(forms, key=lambda form: (rank.get(form.id, len(rank)), form.id))
    hidden: list[str] = []
    visible_ids = {form.id for form in sorted_forms}
    hidden_ids: set[str] = set()
    for rule in form_policy.visibility_rules:
        if rule.packet_profile != runtime_profile.packet_profile:
            continue
        if rule.hide_only_when_form_ids_available and not set(rule.hide_only_when_form_ids_available) <= visible_ids:
            continue
        hidden_ids.update(rule.hidden_form_ids)
    if hidden_ids:
        visible = []
        for form in sorted_forms:
            if form.id in hidden_ids:
                hidden.append(form.id)
                continue
            visible.append(form)
        sorted_forms = visible
    return sorted_forms, hidden


def _apply_packet_limit(
    packet: dict[str, Any],
    *,
    forms: list[MailboxFormSpec],
    hidden_form_ids: list[str],
    packet_profile: str,
    detail: str,
    spec_root: Path,
) -> None:
    if str(detail or "compact").strip().lower() == "full":
        return
    form_policy = load_mailbox_form_policy_spec(spec_root=spec_root)
    limit = next((item for item in form_policy.packet_limits if item.packet_profile == packet_profile), None)
    if limit is None or limit.max_forms <= 0 or len(forms) <= limit.max_forms:
        return
    visible_forms = forms[: limit.max_forms]
    omitted_forms = [form.id for form in forms[limit.max_forms :]]
    packet["forms"] = [_public_form_payload(form) for form in visible_forms]
    packet["hidden_forms"] = list(dict.fromkeys([*hidden_form_ids, *omitted_forms]))
    packet["omitted_forms"] = omitted_forms
    packet["details_available"] = True
    packet["packet_profile"] = packet_profile
    packet["packet_limit"] = {"max_forms": limit.max_forms, "reason": limit.reason}
    packet["next_safe_action"] = _next_safe_action(str(packet.get("state") or ""), visible_forms)


def build_mailbox_submit_receipt(
    *,
    form_id: str,
    payload: dict[str, Any] | None = None,
    state: WorkflowStateName | str = "planning",
    project: str = "mnemoforge",
    runtime_profile_id: str = "unknown_cli",
    diagnostic: bool = False,
    status: str | None = None,
    message: str | None = None,
    data_ref: str = "",
    approved_command: str = "",
    forbidden_patterns: list[str] | None = None,
    spec_root: Path = DEFAULT_SPEC_ROOT,
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    form = mailbox_form_by_id(form_id, spec_root=spec_root)
    state_name = str(state or "planning").strip()
    if state_name not in _WORKFLOW_STATE_NAMES:
        state_name = "planning"
    runtime_profile = _runtime_profile(runtime_profile_id, spec_root=spec_root)

    if form is None:
        return {
            "state": state_name,
            "project": project,
            "receipt": _compact_receipt(
                {
                    "status": "rejected",
                    "form_id": str(form_id or "").strip(),
                    "message": "Unknown mailbox form. Request mailbox_state and choose one of the listed forms.",
                    "submitted_fields": sorted(str(key) for key in payload.keys()),
                    "next_safe_action": "Request mailbox_state for the current workflow state.",
                }
            ),
        }

    if state_name not in mailbox_form_state_names(form):
        return {
            "state": state_name,
            "project": project,
            "receipt": _compact_receipt(
                {
                    "status": "rejected",
                    "form_id": form.id,
                    "message": f"Form {form.id} is not available in state {state_name}.",
                    "available_states": sorted(mailbox_form_state_names(form)),
                    "submitted_fields": sorted(str(key) for key in payload.keys()),
                    "next_safe_action": "Request mailbox_state for this state and submit one of its forms.",
                }
            ),
        }

    missing_fields = [
        field
        for field in form.required_fields
        if payload.get(field) in (None, "", [])
    ]
    if missing_fields:
        return {
            "state": state_name,
            "project": project,
            "receipt": _compact_receipt(
                {
                    "status": "needs_input",
                    "form_id": form.id,
                    "message": "Required fields are missing.",
                    "missing_fields": missing_fields,
                    "submitted_fields": sorted(str(key) for key in payload.keys()),
                    "next_safe_action": "Fill the missing fields and submit the same form again.",
                }
            ),
        }

    if status is None or message is None:
        status, message, data_ref, approved_command, forbidden_patterns = _default_submit_outcome(
            form=form,
            payload=payload,
            state=state_name,
            project=project,
        )

    receipt = {
        "status": status or "accepted",
        "form_id": form.id,
        "mode": form.mode,
        "message": message,
        "data_ref": data_ref,
        "approved_command": approved_command,
        "forbidden_patterns": forbidden_patterns or [],
        "submitted_fields": sorted(str(key) for key in payload.keys()),
        "next_safe_action": "Review the receipt and request the next mailbox_state packet.",
    }
    if form.mode == "write" and status == "needs_review":
        receipt["next_safe_action"] = "Ask Clerk to validate/autofill the form, then review the draft before any governed write."

    packet: dict[str, Any] = {
        "state": state_name,
        "project": project,
        "receipt": _compact_receipt(receipt),
        "next_safe_action": receipt["next_safe_action"],
    }
    if diagnostic and runtime_profile.allow_internal_diagnostics:
        packet["_internal"] = {
            "visibility": "internal",
            "form": _internal_form_payload(form),
            "expected_postconditions": form.postconditions.model_dump(mode="json"),
        }
    return packet


def build_mailbox_mutation_packet(
    *,
    form: MailboxFormSpec,
    payload: dict[str, Any],
    state: WorkflowStateName | str,
    project: str,
    actual_metadata: dict[str, Any],
    result: dict[str, Any],
    runtime_profile_id: str = "unknown_cli",
    diagnostic: bool = False,
    spec_root: Path = DEFAULT_SPEC_ROOT,
) -> dict[str, Any]:
    health = evaluate_mailbox_postconditions(form, actual_metadata)
    status = "accepted" if health["ok"] else "route_unhealthy"
    receipt = {
        "status": status,
        "form_id": form.id,
        "mode": form.mode,
        "message": "Governed mailbox form executed." if health["ok"] else "Internal route result did not satisfy the form contract.",
        "id": result.get("id"),
        "artifact_key": result.get("artifact_key"),
        "stage": result.get("stage"),
        "submitted_fields": sorted(str(key) for key in payload.keys()),
        "next_safe_action": (
            "Request mailbox_state for the next workflow state."
            if health["ok"]
            else "Treat this internal route as unhealthy and use mailbox_state for a safe replacement form."
        ),
    }
    packet: dict[str, Any] = {
        "state": str(state or "planning"),
        "project": project,
        "receipt": _compact_receipt(receipt),
        "next_safe_action": receipt["next_safe_action"],
    }
    runtime_profile = _runtime_profile(runtime_profile_id, spec_root=spec_root)
    if diagnostic and runtime_profile.allow_internal_diagnostics:
        packet["_internal"] = {
            "visibility": "internal",
            "form": _internal_form_payload(form),
            "expected_postconditions": form.postconditions.model_dump(mode="json"),
            "actual_metadata": actual_metadata,
            "postcondition_health": health,
        }
    return packet


def evaluate_mailbox_postconditions(form: MailboxFormSpec, actual_metadata: dict[str, Any]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for key, expected in form.postconditions.expected_metadata.items():
        actual = actual_metadata.get(key)
        if actual != expected:
            failures.append({"type": "expected_mismatch", "field": key, "expected": expected, "actual": actual})
    for key, forbidden_values in form.postconditions.forbidden_metadata.items():
        actual = actual_metadata.get(key)
        if actual in forbidden_values:
            failures.append({"type": "forbidden_value", "field": key, "forbidden": forbidden_values, "actual": actual})
        if isinstance(actual, str) and any(isinstance(value, str) and value in actual for value in forbidden_values):
            failures.append({"type": "forbidden_substring", "field": key, "forbidden": forbidden_values, "actual": actual})
    return {
        "ok": not failures,
        "failures": failures,
        "expected_metadata": form.postconditions.expected_metadata,
        "actual_metadata": actual_metadata,
    }


def mailbox_form_disabled_features(
    form: MailboxFormSpec,
    *,
    project: str,
    runtime_profile_id: str,
    spec_root: Path = DEFAULT_SPEC_ROOT,
) -> set[str]:
    runtime_profile = _runtime_profile(runtime_profile_id, spec_root=spec_root)
    disabled_features = _disabled_feature_ids(
        runtime_profile_id=runtime_profile.id,
        project=project,
        runtime_disabled=set(runtime_profile.default_disabled_features),
        spec_root=spec_root,
    )
    return disabled_features.intersection(form.feature_toggles)


def build_mailbox_get_packet(
    *,
    ref: str,
    state: WorkflowStateName | str = "planning",
    project: str = "mnemoforge",
    runtime_profile_id: str = "unknown_cli",
    diagnostic: bool = False,
    detail: str = "compact",
    spec_root: Path = DEFAULT_SPEC_ROOT,
) -> dict[str, Any]:
    normalized_ref = str(ref or "").strip()
    if not normalized_ref:
        return {
            "state": str(state or "planning"),
            "project": project,
            "receipt": {
                "status": "needs_input",
                "message": "ref is required.",
                "next_safe_action": "Provide a mailbox_state:<project>:<state> reference or request mailbox_state directly.",
            },
        }

    if normalized_ref.startswith("mailbox_state:"):
        parts = normalized_ref.split(":")
        ref_project = parts[1] if len(parts) > 1 and parts[1] else project
        ref_state = parts[2] if len(parts) > 2 and parts[2] else state
        return build_mailbox_state_packet(
            state=ref_state,
            project=ref_project,
            runtime_profile_id=runtime_profile_id,
            diagnostic=diagnostic,
            detail=detail,
            spec_root=spec_root,
        )

    return {
        "state": str(state or "planning"),
        "project": project,
        "receipt": {
            "status": "not_found",
            "message": f"Mailbox reference is not available through the public get surface: {normalized_ref}",
            "next_safe_action": "Request mailbox_state for the current workflow state and continue through public forms.",
        },
    }


def _default_submit_outcome(
    *,
    form: MailboxFormSpec,
    payload: dict[str, Any],
    state: str,
    project: str,
) -> tuple[str, str, str, str, list[str]]:
    if form.id == "run_verification":
        requested = payload.get("requested_checks")
        checks = " ".join(str(item) for item in requested) if isinstance(requested, list) and requested else ""
        pytest_args = checks.strip() or "tests\\test_mcp_workflow_specs.py tests\\test_mcp_sse.py -k mailbox -q"
        return (
            "ready",
            "Use the project-approved Docker verification contour. Host pytest is forbidden for this project.",
            "",
            f"./scripts/run_pytest_docker.ps1 -NoBuild {pytest_args}",
            ["python -m pytest", "pytest", "host execution_context"],
        )

    if form.id == "get_task_context":
        return (
            "accepted",
            "Read-only task context request accepted by the mailbox layer.",
            f"mailbox_state:{project}:{state}",
            "",
            [],
        )

    if form.mode == "write":
        clerk_hint = " Clerk can validate/autofill this form before the governed write." if form.assistance.clerk_available else ""
        return (
            "needs_review",
            f"Mutating mailbox_submit is guarded in this migration slice; no write was performed.{clerk_hint}",
            "",
            "",
            [],
        )

    return (
        "accepted",
        "Form accepted by the mailbox layer.",
        f"mailbox_state:{project}:{state}",
        "",
        [],
    )


def _compact_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in receipt.items()
        if value is not None and value != "" and value != []
    }


def _disabled_feature_ids(
    *,
    runtime_profile_id: str,
    project: str,
    runtime_disabled: set[str],
    spec_root: Path,
) -> set[str]:
    registry = load_feature_toggle_registry(spec_root=spec_root)
    try:
        from app.services.mcp_feature_gates import get_mcp_feature_gate_store

        gate_store = get_mcp_feature_gate_store()
    except Exception:
        gate_store = None
    disabled: set[str] = set()
    for toggle in registry.toggles:
        default_enabled = bool(toggle.default_enabled) and toggle.id not in runtime_disabled
        enabled = default_enabled
        if gate_store is not None:
            enabled = gate_store.is_enabled(
                feature_id=toggle.id,
                default_enabled=default_enabled,
                scope_chain=[
                    ("runtime_profile", runtime_profile_id),
                    ("project", project),
                    ("global", "global"),
                ],
            )
        if not enabled:
            disabled.add(toggle.id)
    return disabled


def _next_safe_action(state: str, forms: list[MailboxFormSpec]) -> str:
    if state == "verification":
        return "Submit run_verification to get the project-approved verification contour."
    if state == "checkpointing" and any(form.id == "finish_task" for form in forms):
        return "Submit finish_task when closeout evidence is ready, or record_progress if work should continue."
    if state == "planning" and any(form.id == "get_task_context" for form in forms):
        return "Submit get_task_context first, then start_task when task identity and scope are clear."
    if any(form.id == "start_task" for form in forms):
        return "Submit start_task before editing when you are beginning real implementation work."
    if any(form.id == "claim_task" for form in forms):
        return "Submit claim_task before editing when you are taking ownership of a task."
    if any(form.id == "get_task_context" for form in forms):
        return "Submit get_task_context before choosing the next action if context is incomplete."
    if forms:
        return f"Submit {forms[0].id} with the required fields."
    return "No public forms are available for this state."
