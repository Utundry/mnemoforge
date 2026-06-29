from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.developer_feedback_packet_service import build_developer_feedback_packet
from app.services.evidence_classification_service import classify_evidence_items
from app.services.edit_authority_service import build_edit_authority
from app.services.autonomous_mode_service import (
    evaluate_autonomous_mode,
    get_autonomous_mode_store,
    normalize_autonomous_mode,
)
from app.services.diagnostic_inspection_service import build_diagnostic_inspection_packet
from app.services.mcp_mailbox import (
    build_mailbox_mutation_packet,
    build_mailbox_submit_receipt,
    evaluate_mailbox_postconditions,
    mailbox_form_by_id,
    mailbox_form_disabled_features,
    mailbox_form_state_names,
)
from app.services.mcp_tool_contracts import build_report_task_checkpoint_payload
from app.services.mcp_workflow_specs import load_named_json_spec, load_route_catalog_spec
from app.services.mcp_simple_read_actions import (
    PublicRefDependencies,
    compact_public_ref_matches,
    resolve_public_artifact_short_ref,
)
from app.services.knowledge_refinement_service import (
    build_knowledge_refinement_packet,
    build_knowledge_refinement_request,
)
from app.services.governed_refinement_lifecycle import (
    build_refinement_lifecycle,
    complete_refinement_lifecycle,
)
from app.services.stage_applicability_service import stage_allows_block
from app.services.public_ref_index import AmbiguousPublicRefError, is_short_public_id
from app.services.public_diagnostic_service import attach_public_diagnostic_incident
from app.services.route_pattern_store import get_route_pattern_store
from app.services.authority_classification_service import classify_improvement_authority


PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PatchCallback = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]
ExecuteToolCallback = Callable[[str, dict[str, Any], str, str | None], Awaitable[str]]
SessionIdentityCallback = Callable[[str | None], Awaitable[dict[str, str]]]
TaskMutationGuardCallback = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class MailboxActionDependencies:
    post: PostCallback
    execute_tool: ExecuteToolCallback
    get_session_identity_defaults: SessionIdentityCallback
    task_mutation_guard: TaskMutationGuardCallback
    get: GetCallback | None = None
    patch: PatchCallback | None = None


async def build_mailbox_submit_packet(
    *,
    args: dict[str, Any],
    payload: dict[str, Any],
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    form_id = str(args.get("form_id") or "").strip()
    state = str(args.get("state") or "planning").strip() or "planning"
    project = str(args.get("project") or payload.get("project") or "mnemoforge").strip() or "mnemoforge"
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    runtime_profile_id = str(args.get("runtime_profile_id") or identity_defaults.get("runtime_profile_id") or "unknown_cli")
    diagnostic = bool(args.get("diagnostic", False))

    preflight = build_mailbox_submit_receipt(
        form_id=form_id,
        payload=payload,
        state=state,
        project=project,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    if preflight.get("receipt", {}).get("status") in {"rejected", "needs_input"}:
        return preflight

    form = mailbox_form_by_id(form_id)
    if form is None or state not in mailbox_form_state_names(form):
        return preflight
    disabled_features = mailbox_form_disabled_features(
        form,
        project=project,
        runtime_profile_id=runtime_profile_id,
    )
    if disabled_features:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "feature_disabled",
                "form_id": form.id,
                "message": "This mailbox form depends on disabled functionality.",
                "disabled_features": sorted(disabled_features),
                "replacement_form_ids": form.replacement_form_ids,
                "next_safe_action": "Request mailbox_state and choose an available replacement form.",
            },
        }

    common = {
        "form": form,
        "payload": payload,
        "state": state,
        "project": project,
        "runtime_profile_id": runtime_profile_id,
        "diagnostic": diagnostic,
    }
    if form_id == "get_task_context":
        return await mailbox_get_task_context(
            **common,
            api_base=api_base,
            dependencies=dependencies,
            session_id=session_id,
        )
    if form_id == "create_improvement":
        return await mailbox_create_improvement(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )
    if form_id == "store_memory":
        return await mailbox_store_memory(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )
    if form_id == "create_law":
        return await mailbox_create_law(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )
    if form_id == "confirm_law":
        return await mailbox_confirm_law(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )
    if form_id == "record_progress":
        return await mailbox_record_progress(
            **common,
            api_base=api_base,
            dependencies=dependencies,
            session_id=session_id,
        )
    if form_id == "claim_task":
        return await mailbox_claim_task(**common, dependencies=dependencies, session_id=session_id)
    if form_id == "start_task":
        return await mailbox_start_task(
            **common,
            api_base=api_base,
            dependencies=dependencies,
            session_id=session_id,
        )
    if form_id == "release_task_claim":
        return await mailbox_release_task_claim(**common, session_id=session_id)
    if form_id == "finish_task":
        return await mailbox_finish_task(
            **common,
            api_base=api_base,
            dependencies=dependencies,
            session_id=session_id,
        )
    if form_id == "close_task":
        return await mailbox_close_task(
            **common,
            api_base=api_base,
            dependencies=dependencies,
            session_id=session_id,
        )
    if form_id == "set_feature_gate":
        return mailbox_set_feature_gate(**common)
    if form_id == "diagnostic_inspection":
        return mailbox_diagnostic_inspection(**common)
    if form_id == "developer_feedback_packet":
        return mailbox_developer_feedback_packet(**common)
    if form_id == "route_hygiene":
        return mailbox_route_hygiene(**common)
    if form_id == "route_feedback":
        return mailbox_route_feedback(**common)
    if form_id == "knowledge_refinement_feedback":
        return await mailbox_knowledge_refinement_feedback(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )
    if form_id == "upsert_context_page":
        return await mailbox_upsert_context_page(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )
    if form_id == "archive_context_page":
        return await mailbox_archive_context_page(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )
    if form_id == "review_task_reconciliation":
        return await mailbox_review_task_reconciliation(
            **common,
            api_base=api_base,
            dependencies=dependencies,
        )

    return preflight


async def mailbox_upsert_context_page(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    page_id = str(payload.get("page_id") or "").strip()
    body = {
        "project": project,
        "parent_ref": str(payload.get("parent_ref") or "").strip(),
        "page_kind": str(payload.get("page_kind") or "entry").strip() or "entry",
        "page_index": int(payload.get("page_index") or 1),
        "title": str(payload.get("title") or "").strip(),
        "summary": str(payload.get("summary") or "").strip(),
        "content": str(payload.get("content") or ""),
        "created_by": str(payload.get("created_by") or payload.get("updated_by") or "codex").strip() or "codex",
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }
    if page_id:
        if dependencies.patch is None:
            return _mailbox_context_page_error(form=form, state=state, project=project, message="Context page update route is unavailable.")
        result = await dependencies.patch(
            api_base,
            f"/context-pages/{quote(page_id, safe='')}",
            {
                "title": body["title"] or None,
                "summary": body["summary"] or None,
                "content": body["content"] or None,
                "updated_by": str(payload.get("updated_by") or body["created_by"]),
                "metadata": body["metadata"],
            },
        )
        action = "superseded"
    else:
        result = await dependencies.post(api_base, "/context-pages", body)
        action = "created"
    result["id"] = result.get("page_id")
    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata={"result_kind": "context_page", "mutation": True, "review_mode": False, "route_id": "mailbox.context_page.upsert.v1"},
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    packet["receipt"].update(_compact({
        "message": f"Context page {action}.",
        "page_ref": result.get("page_ref"),
        "page_id": result.get("page_id"),
        "parent_ref": result.get("parent_ref"),
        "page_kind": result.get("page_kind"),
        "page_index": result.get("page_index"),
        "version": result.get("version"),
        "next_safe_action": "Use get with the returned context_page ref to read this page, or continue the approved workflow.",
    }))
    packet["next_safe_action"] = packet["receipt"]["next_safe_action"]
    return packet


async def mailbox_archive_context_page(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    page_id = str(payload.get("page_id") or "").strip()
    if not page_id:
        return _mailbox_context_page_error(form=form, state=state, project=project, message="page_id is required.")
    result = await dependencies.post(
        api_base,
        f"/context-pages/{quote(page_id, safe='')}/archive",
        {
            "updated_by": str(payload.get("updated_by") or "codex").strip() or "codex",
            "reason": str(payload.get("reason") or "").strip(),
        },
    )
    result["id"] = result.get("page_id")
    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata={"result_kind": "context_page", "mutation": True, "review_mode": False, "route_id": "mailbox.context_page.archive.v1"},
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    packet["receipt"].update(_compact({
        "message": "Context page archived.",
        "page_ref": result.get("page_ref"),
        "page_id": result.get("page_id"),
        "parent_ref": result.get("parent_ref"),
        "next_safe_action": "Ordinary retrieval will now exclude this page; use include_history only for audit/history.",
    }))
    packet["next_safe_action"] = packet["receipt"]["next_safe_action"]
    return packet


def _mailbox_context_page_error(*, form, state: str, project: str, message: str) -> dict[str, Any]:
    return {
        "state": state,
        "project": project,
        "receipt": {
            "status": "needs_input",
            "form_id": form.id,
            "message": message,
            "next_safe_action": "Submit the context page form again with the required fields.",
        },
    }


async def mailbox_review_task_reconciliation(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    result = await dependencies.post(
        api_base,
        "/task-reconciliation/review",
        {
            "target_task_ref": str(payload.get("target_task_ref") or "").strip(),
            "implemented_task_ref": str(payload.get("implemented_task_ref") or "").strip(),
            "decision": str(payload.get("decision") or "").strip(),
            "reason": str(payload.get("reason") or "").strip(),
            "acted_by": str(payload.get("acted_by") or "codex").strip() or "codex",
            "evidence_refs": payload.get("evidence_refs") if isinstance(payload.get("evidence_refs"), list) else [],
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        },
    )
    decision_record = result.get("decision_record") if isinstance(result.get("decision_record"), dict) else {}
    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata={"result_kind": "task_reconciliation", "mutation": True, "review_mode": True, "route_id": "mailbox.task_reconciliation.review.v1"},
        result={"id": decision_record.get("decision_id"), **result},
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    packet["receipt"].update(_compact({
        "decision_id": decision_record.get("decision_id"),
        "target_ref": decision_record.get("target_task_ref"),
        "decision": decision_record.get("decision"),
        "implemented_task_ref": decision_record.get("implemented_task_ref"),
        "next_safe_action": "Use get/query for the reconciliation packet or request next priority again.",
    }))
    packet["result"] = result.get("packet")
    packet["next_safe_action"] = packet["receipt"]["next_safe_action"]
    return packet


async def mailbox_get_task_context(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
) -> dict[str, Any]:
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "needs_input",
                "form_id": form.id,
                "message": "task_id is required to fetch a concrete task context.",
                "missing_fields": ["task_id"],
                "next_safe_action": "Use project_work with intent 'list active tasks' to choose a task_id, then submit get_task_context with that task_id.",
            },
        }

    detail = str(payload.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    try:
        raw = await dependencies.execute_tool(
            "pull_task_context",
            {
                "project": project,
                "task_id": task_id,
                "detail": detail,
                "include_handoffs": True,
                "limit": int(payload.get("limit") or 10),
                "source": "mailbox_submit.get_task_context",
            },
            api_base,
            session_id,
        )
    except Exception as exc:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "not_found",
                "form_id": form.id,
                "message": public_mailbox_error_message(exc),
                "next_safe_action": "Use project_work with intent 'list active tasks' to verify the task_id, then submit get_task_context again.",
            },
        }

    try:
        result = json.loads(raw)
    except Exception:
        result = {"status": "ok", "text": raw}
    actual_metadata = {"result_kind": "state_data", "mutation": False, "internal_tool": "pull_task_context"}
    health = evaluate_mailbox_postconditions(form, actual_metadata)
    receipt = {
        "status": "accepted",
        "form_id": form.id,
        "mode": form.mode,
        "message": "Task context fetched.",
        "task_id": task_id,
        "data_ref": f"task:{project}:{task_id}",
        "next_safe_action": result.get("next_safe_action") or "Review task context before claiming or editing.",
    }
    packet: dict[str, Any] = {
        "state": state,
        "project": project,
        "receipt": _compact(receipt),
        "result": result,
        "next_safe_action": receipt["next_safe_action"],
    }
    if diagnostic:
        packet["_internal"] = {"visibility": "internal", "actual_metadata": actual_metadata, "postcondition_health": health}
    return packet


async def _build_public_verification_policy(
    *,
    result: dict[str, Any],
    project: str,
    task_id: str,
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
) -> dict[str, Any]:
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    task_title = str(task.get("title") or result.get("title") or task_id).strip()
    try:
        raw = await dependencies.execute_tool(
            "get_task_execution_context",
            {
                "project": project,
                "task_id": task_id,
                "task": task_title,
                "state": "verification",
                "include_rules": True,
                "include_tools": False,
                "max_required_rules": 3,
                "max_recommended_rules": 3,
            },
            api_base,
            session_id,
        )
        context = json.loads(raw)
    except Exception:
        return {
            "status": "unavailable",
            "next_safe_action": "Request project verification context before executing checks.",
        }
    if not isinstance(context, dict):
        return {
            "status": "unavailable",
            "next_safe_action": "Request project verification context before executing checks.",
        }
    readiness = context.get("readiness") if isinstance(context.get("readiness"), dict) else {}
    return _compact(
        {
            "status": "ready" if readiness.get("ready_to_enter") else "needs_review",
            "state": "verification",
            "readiness": {
                "ready_to_enter": readiness.get("ready_to_enter"),
                "missing_prerequisites": readiness.get("missing_prerequisites") or [],
                "required_actions": readiness.get("required_actions") or [],
            }
            if readiness
            else None,
            "required_rules": _compact_rule_refs(context.get("required_rules")),
            "recommended_rules": _compact_rule_refs(context.get("recommended_rules")),
            "risk_controls": list(context.get("risk_controls") or [])[:5] if isinstance(context.get("risk_controls"), list) else [],
            "next_safe_action": "Use the project-approved verification contour before reporting success.",
        }
    )


async def _build_start_task_work_guidance(
    *,
    result: dict[str, Any],
    project: str,
    task_id: str,
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
    edit_authority: dict[str, Any] | None = None,
    guidance_stage: str = "implementation",
) -> dict[str, Any]:
    if not stage_allows_block("work_guidance", state=guidance_stage):
        return {}
    guidance: dict[str, Any] = {
        "message": "Begin work; remember the project-specific execution policy before implementation and verification.",
    }
    if edit_authority and stage_allows_block("edit_authority", state=guidance_stage):
        guidance["edit_authority"] = edit_authority
    if stage_allows_block("verification_policy", state=guidance_stage):
        guidance["verification_policy"] = await _build_public_verification_policy(
            result=result,
            project=project,
            task_id=task_id,
            api_base=api_base,
            dependencies=dependencies,
            session_id=session_id,
        )
    if stage_allows_block("checkpoint_reminder", state=guidance_stage):
        guidance["checkpoint_reminder"] = (
            "After a meaningful work slice, submit record_progress; when closing claimed work, submit finish_task."
        )
    return _compact(guidance)


def _compact_rule_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        refs.append(
            _compact(
                {
                    "id": item.get("id") or item.get("law_id"),
                    "ref": _rule_ref_from_item(item),
                    "expand_ref": _rule_ref_from_item(item),
                    "title": item.get("title"),
                    "scope": item.get("scope"),
                    "topic_path": item.get("topic_path"),
                    "reason": item.get("reason"),
                }
            )
        )
        if len(refs) >= 3:
            break
    return refs


def _rule_ref_from_item(item: dict[str, Any]) -> str:
    law_id = str(item.get("id") or item.get("law_id") or "").strip()
    if not law_id:
        return ""
    project = str(item.get("project") or "").strip()
    return f"law:{project}:{law_id}" if project else f"law:{law_id}"


def public_lease_payload(lease: dict[str, Any] | None) -> dict[str, Any]:
    public = dict(lease or {})
    public.pop("work_token_hash", None)
    return public


def public_mailbox_error_message(exc: Exception) -> str:
    text = str(exc)
    lowered = text.casefold()
    if "404" in text or "not found" in lowered:
        return "Requested task or route was not found."
    if "401" in text or "403" in text or "unauthorized" in lowered or "forbidden" in lowered:
        return "Mailbox action was not authorized for the current session."
    if "closeout_required" in lowered or "closeout evidence" in lowered:
        return "Completed work requires explicit closeout evidence: verification, changed_files, and next_step."
    return "Mailbox action could not be completed by the server."


async def mailbox_claim_task(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
) -> dict[str, Any]:
    from app.services.task_lease_service import TaskLeaseConflict, WorkTokenMismatch, get_task_lease_store

    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    owner_agent = str(payload.get("owner_agent") or payload.get("agent_id") or "codex").strip() or "codex"
    lease_session_id = str(payload.get("session_id") or session_id or "").strip()
    if not lease_session_id:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "needs_input",
                "form_id": form.id,
                "message": "session_id is required for task claim unless the MCP session provides one.",
                "missing_fields": ["session_id"],
                "next_safe_action": "Submit claim_task again with session_id or reconnect through MCP so the server can bind the session.",
            },
        }
    agent_fingerprint = str(payload.get("agent_fingerprint") or identity_defaults.get("agent_fingerprint") or "").strip()
    effective_profile = str(
        payload.get("runtime_profile_id") or identity_defaults.get("runtime_profile_id") or runtime_profile_id or "unknown_cli"
    ).strip() or "unknown_cli"
    try:
        claim = get_task_lease_store().claim(
            project=project,
            task_id=str(payload["task_id"]),
            owner_agent=owner_agent,
            session_id=lease_session_id,
            agent_fingerprint=agent_fingerprint,
            runtime_profile_id=effective_profile,
            work_token=str(payload.get("work_token") or ""),
            lease_ttl_seconds=int(payload.get("lease_ttl_seconds") or 900),
        )
    except TaskLeaseConflict as exc:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "conflict",
                "form_id": form.id,
                "message": "Task is already claimed by another active session.",
                "lease": exc.active_lease.model_dump(mode="json"),
                "next_safe_action": "Request get_task_context, coordinate with the owner, or wait for lease timeout.",
            },
        }
    except WorkTokenMismatch as exc:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "conflict",
                "form_id": form.id,
                "message": "work_token did not match the active same-fingerprint claim.",
                "lease_id": exc.lease_id,
                "next_safe_action": "Do not reclaim this task; recover the correct work_token or coordinate with the owner.",
            },
        }

    result = claim.model_dump(mode="json")
    actual_metadata = {"result_kind": "task_claim", "mutation": True}
    health = evaluate_mailbox_postconditions(form, actual_metadata)
    receipt = {
        "status": claim.status,
        "form_id": form.id,
        "mode": form.mode,
        "message": "Task claim is active.",
        "lease": public_lease_payload(result.get("lease")),
        "work_token": result.get("work_token"),
        "same_fingerprint_reclaim": result.get("same_fingerprint_reclaim"),
        "previous_claim_expired": result.get("previous_claim_expired"),
        "next_safe_action": "Proceed with implementation and keep work_token for checkpoints, finish, or recovery.",
    }
    packet: dict[str, Any] = {
        "state": state,
        "project": project,
        "receipt": _compact(receipt),
        "next_safe_action": receipt["next_safe_action"],
    }
    if diagnostic:
        packet["_internal"] = {"visibility": "internal", "actual_metadata": actual_metadata, "postcondition_health": health}
    return packet


async def mailbox_start_task(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
) -> dict[str, Any]:
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    lease_session_id = str(payload.get("session_id") or session_id or _generated_mailbox_session_id(payload))
    mode_store = get_autonomous_mode_store()
    supplied_mode = payload.get("autonomous_mode") if isinstance(payload.get("autonomous_mode"), dict) else None
    if supplied_mode is not None and str(supplied_mode.get("mode") or "") == "collaborative_control":
        mode_store.revoke(session_id=lease_session_id, project=project)
    stored_mode = mode_store.get(session_id=lease_session_id, project=project)
    candidate_mode = normalize_autonomous_mode(supplied_mode) if supplied_mode is not None else stored_mode
    candidate_framing_version = str(payload.get("framing_version") or "").strip()
    if not candidate_framing_version and isinstance(candidate_mode, dict):
        candidate_framing_version = str(
            (candidate_mode.get("task_framing_versions") or {}).get(str(payload["task_id"])) or ""
        ).strip()
    autonomous_mode = evaluate_autonomous_mode(
        candidate_mode,
        task_id=str(payload["task_id"]),
        action="start_task",
        framing_version=candidate_framing_version,
    )
    explicit_framing = str(payload.get("approved_framing") or "").strip()
    approval_intent = str(payload.get("approval_intent") or "user_approved_start").strip()
    explicit_user_approval = bool(explicit_framing and approval_intent == "user_approved_start")
    candidate_is_autonomous = bool(normalize_autonomous_mode(candidate_mode).get("active"))
    if candidate_is_autonomous and not autonomous_mode.get("authority_granted") and not explicit_user_approval:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "authority_denied",
                "form_id": form.id,
                "message": "The explicit autonomous-mode grant does not authorize this task start.",
                "autonomous_mode": autonomous_mode,
                "diagnostic_incident": autonomous_mode.get("diagnostic_incident"),
                "next_safe_action": autonomous_mode["next_safe_action"],
            },
            "next_safe_action": autonomous_mode["next_safe_action"],
        }

    has_prior_claim_or_progress = False
    if not explicit_user_approval and not autonomous_mode.get("authority_granted"):
        has_prior_claim_or_progress = await _task_has_prior_claim_or_progress(
            api_base=api_base,
            project=project,
            task_id=str(payload["task_id"]),
            dependencies=dependencies,
        )
        if not has_prior_claim_or_progress:
            return _start_task_framing_required_packet(
                state=state,
                project=project,
                form_id=form.id,
                task_id=str(payload["task_id"]),
                message="Starting implementation requires the latest full task statement to be explicitly approved.",
                linked_artifact_key="",
            )

    if dependencies.get is not None:
        try:
            task = await dependencies.get(
                api_base,
                f"/project/tasks/{quote(str(payload['task_id']), safe='')}?project={quote(project, safe='')}",
            )
        except Exception:
            task = {}
        if (
            isinstance(task, dict)
            and task.get("linked_improvement_id")
            and bool(task.get("task_statement_incomplete"))
            and not has_prior_claim_or_progress
            and not (
                (explicit_framing and approval_intent == "user_approved_start")
                or autonomous_mode.get("authority_granted")
            )
        ):
            return _start_task_framing_required_packet(
                state=state,
                project=project,
                form_id=form.id,
                task_id=str(payload["task_id"]),
                message="This task is a technical projection of an improvement and is not ready for implementation.",
                linked_artifact_key=f"improvement:{project}:{task['linked_improvement_id']}",
            )
    start_args = {
        **payload,
        "project": project,
        "task_id": str(payload["task_id"]),
        "owner_agent": str(payload.get("owner_agent") or payload.get("agent_id") or "codex"),
        "session_id": lease_session_id,
        "agent_fingerprint": str(payload.get("agent_fingerprint") or identity_defaults.get("agent_fingerprint") or ""),
        "runtime_profile_id": str(payload.get("runtime_profile_id") or identity_defaults.get("runtime_profile_id") or runtime_profile_id or "unknown_cli"),
        "reason": str(payload.get("reason") or "mailbox_submit.start_task"),
        "source": str(payload.get("source") or "mailbox_submit.start_task"),
    }
    try:
        raw = await dependencies.execute_tool("start_task_session", start_args, api_base, session_id)
    except Exception as exc:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "conflict",
                "form_id": form.id,
                "message": public_mailbox_error_message(exc),
                "next_safe_action": "Request get_task_context, verify the task_id exists, or create/choose a valid task before starting work.",
            },
        }
    try:
        result = json.loads(raw)
    except Exception:
        result = {"status": "error", "message": raw}
    if result.get("status") != "started":
        return _start_task_conflict_packet(
            state=state,
            project=project,
            form_id=form.id,
            payload=payload,
            result=result,
        )
    actual_metadata = {"result_kind": "task_started", "mutation": True}
    health = evaluate_mailbox_postconditions(form, actual_metadata)
    previous_lease = public_lease_payload(result.get("previous_lease"))
    reclaim = _start_task_reclaim_payload(result=result, previous_lease=previous_lease)
    reclaimed_after_ttl = reclaim.get("reason") == "previous_lease_expired"
    resumed = bool(result.get("work_session_resumed"))
    if supplied_mode is not None and autonomous_mode.get("authority_granted"):
        mode_store.save(session_id=lease_session_id, project=project, grant=normalize_autonomous_mode(supplied_mode))
    approved_framing = str(payload.get("approved_framing") or "").strip()
    edit_authority = build_edit_authority(
        state="implementation",
        task_id=str(payload["task_id"]),
        approved_framing=approved_framing,
        framing_version=candidate_framing_version,
        approval_intent=str(payload.get("approval_intent") or "user_approved_start").strip(),
        autonomous_mode=autonomous_mode,
    )
    work_guidance = await _build_start_task_work_guidance(
        result=result,
        project=project,
        task_id=str(payload["task_id"]),
        api_base=api_base,
        dependencies=dependencies,
        session_id=session_id,
        edit_authority=edit_authority,
    )
    receipt = {
        "status": "reclaimed_after_ttl" if reclaimed_after_ttl else "started",
        "form_id": form.id,
        "mode": form.mode,
        "message": (
            "Task lease reclaimed after TTL expiry; the same-fingerprint work session is ready."
            if reclaimed_after_ttl
            else "Task session resumed."
            if resumed
            else "Task session started."
        ),
        "lease": public_lease_payload(result.get("lease")),
        "work_handle": result.get("work_handle"),
        "work_token": result.get("work_token"),
        "work_session": result.get("work_session"),
        "work_session_resumed": resumed,
        "edit_authority": edit_authority,
        "autonomous_mode": autonomous_mode,
        "reclaim": reclaim,
        "auto_heartbeat": result.get("auto_heartbeat"),
        "work_guidance": work_guidance,
        "next_state": "implementation",
        "next_forms": ["record_progress", "finish_task", "release_task_claim"],
        "next_safe_action": "Continue implementation, then submit record_progress or finish_task through mailbox using the returned work_handle.",
    }
    packet: dict[str, Any] = {"state": state, "project": project, "receipt": _compact(receipt), "next_safe_action": receipt["next_safe_action"]}
    if diagnostic:
        packet["_internal"] = {"visibility": "internal", "actual_metadata": actual_metadata, "postcondition_health": health}
    return packet


def _start_task_framing_required_packet(
    *,
    state: str,
    project: str,
    form_id: str,
    task_id: str,
    message: str,
    linked_artifact_key: str = "",
) -> dict[str, Any]:
    next_safe_action = (
        "Stop and show the full task statement to the operator. Submit start_task only after explicit "
        "user_approved_start with approved_framing, unless explicit_autonomous_mode authorizes this task."
    )
    receipt = {
        "status": "framing_required",
        "form_id": form_id,
        "message": message,
        "task_id": task_id,
        "implementation_ready": False,
        "claim_allowed": False,
        "approval_required_before_claim": True,
        "approval_intent": "user_approved_start",
        "autonomous_override": "explicit_autonomous_mode",
        "required_field": "approved_framing",
        "next_safe_action": next_safe_action,
    }
    if linked_artifact_key:
        receipt["linked_artifact_key"] = linked_artifact_key
    return {
        "state": state,
        "project": project,
        "receipt": receipt,
        "next_safe_action": next_safe_action,
    }


async def _task_has_prior_claim_or_progress(
    *,
    api_base: str,
    project: str,
    task_id: str,
    dependencies: MailboxActionDependencies,
) -> bool:
    if dependencies.get is None:
        return False
    try:
        changes = await dependencies.get(
            api_base,
            f"/project/tasks/{quote(task_id, safe='')}/changes?project={quote(project, safe='')}&limit=100",
        )
    except Exception:
        return False
    if not isinstance(changes, list):
        return False
    for change in changes:
        if not isinstance(change, dict):
            continue
        tags = {str(tag or "").strip().casefold() for tag in (change.get("tags") or [])}
        content = str(change.get("content") or "").casefold()
        reason = str(change.get("reason") or change.get("why") or "").casefold()
        source = str(change.get("source") or "").casefold()
        haystack = " ".join((content, reason, source))
        if "task_checkpoint" not in tags and "[task_checkpoint]" not in haystack:
            continue
        if (
            "start_task_session" in haystack
            or "mailbox_submit.start_task" in haystack
            or "task claimed" in haystack
            or "work session started" in haystack
            or "task_stage:in_progress" in tags
            or "checkpoint stage: in_progress" in haystack
        ):
            return True
    return False


async def mailbox_release_task_claim(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    session_id: str | None,
) -> dict[str, Any]:
    from app.services.task_lease_service import TaskLeaseUnavailable, get_task_lease_store, stop_task_lease_auto_heartbeat

    store = get_task_lease_store()
    owner_agent = str(payload.get("owner_agent") or payload.get("agent_id") or "codex").strip() or "codex"
    lease_session_id = str(payload.get("session_id") or session_id or "").strip()
    lease_id = str(payload.get("lease_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not lease_id:
        if not task_id:
            return _needs_input(state, project, form.id, "Provide lease_id or task_id so the server can identify the active claim.", ["lease_id_or_task_id"], "Submit release_task_claim again with lease_id from claim_task receipt or with task_id.")
        active = store.get_active_claim(project=project, task_id=task_id)
        if active is None:
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "not_found",
                    "form_id": form.id,
                    "message": "No active claim exists for this task.",
                    "next_safe_action": "No release is needed; request mailbox_state for the next workflow state.",
                },
            }
        lease_id = active.lease_id
        if not lease_session_id:
            lease_session_id = active.session_id
    if not lease_session_id:
        return _needs_input(state, project, form.id, "session_id is required to release a claim.", ["session_id"], "Submit release_task_claim again with the owner session_id.")
    try:
        released = store.release(
            lease_id=lease_id,
            owner_agent=owner_agent,
            session_id=lease_session_id,
            reason=str(payload.get("reason") or "released_by_mailbox"),
            status=str(payload.get("status") or "released"),
        )
        stop_task_lease_auto_heartbeat(released.lease_id)
    except PermissionError:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "conflict",
                "form_id": form.id,
                "message": "Claim owner/session did not match; release was rejected.",
                "next_safe_action": "Coordinate with the claim owner or wait for lease timeout.",
            },
        }
    except TaskLeaseUnavailable as exc:
        released = exc.lease
    actual_metadata = {"result_kind": "task_claim_released", "mutation": True}
    health = evaluate_mailbox_postconditions(form, actual_metadata)
    receipt = {
        "status": released.status,
        "form_id": form.id,
        "mode": form.mode,
        "message": "Task claim is no longer active.",
        "lease": public_lease_payload(released.model_dump(mode="json")),
        "next_safe_action": "Request mailbox_state for the next workflow state.",
    }
    packet: dict[str, Any] = {"state": state, "project": project, "receipt": _compact(receipt), "next_safe_action": receipt["next_safe_action"]}
    if diagnostic:
        packet["_internal"] = {"visibility": "internal", "actual_metadata": actual_metadata, "postcondition_health": health}
    return packet


async def mailbox_finish_task(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
) -> dict[str, Any]:
    finish_args = {
        **payload,
        "project": project,
        "task_id": str(payload["task_id"]),
        "owner_agent": str(payload.get("owner_agent") or payload.get("agent_id") or "codex"),
        "session_id": str(payload.get("session_id") or session_id or ""),
        "reason": str(payload.get("reason") or "mailbox_submit.finish_task"),
        "source": str(payload.get("source") or "mailbox_submit.finish_task"),
        "release_reason": str(payload.get("release_reason") or "finished_by_mailbox"),
    }
    try:
        raw = await dependencies.execute_tool("finish_task_session", finish_args, api_base, session_id)
    except Exception as exc:
        receipt = {
            "status": "conflict",
            "form_id": form.id,
            "message": public_mailbox_error_message(exc),
            "next_safe_action": "Review closeout evidence and task identity, then submit finish_task again or release_task_claim if only cleanup is needed.",
        }
        return {
            "state": state,
            "project": project,
            "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="mailbox_action_conflict"),
        }
    try:
        result = json.loads(raw)
    except Exception:
        result = {"status": "error", "message": raw}
    if result.get("status") != "finished":
        receipt = {
            "status": result.get("status") or "conflict",
            "form_id": form.id,
            "message": result.get("message") or result.get("error") or "finish_task_session did not finish.",
            "next_safe_action": result.get("next_safe_action") or "Review the receipt, fix missing evidence, and submit finish_task again.",
        }
        return {
            "state": state,
            "project": project,
            "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="mailbox_action_conflict"),
        }
    release = dict(result.get("release") or {})
    if isinstance(release.get("lease"), dict):
        release["lease"] = public_lease_payload(release.get("lease"))
    continuity_lease = result.get("continuity_lease") if isinstance(result.get("continuity_lease"), dict) else None
    if continuity_lease:
        continuity_lease = public_lease_payload(continuity_lease)
    evidence_classification = classify_evidence_items(_string_list_arg(payload.get("verification")))
    actual_metadata = {"result_kind": "task_finished", "mutation": True}
    health = evaluate_mailbox_postconditions(form, actual_metadata)
    receipt = {
        "status": "finished",
        "form_id": form.id,
        "mode": form.mode,
        "message": "Task session finished and claim release was attempted.",
        "task_id": result.get("task_id"),
        "release": release,
        "work_session": result.get("work_session") if isinstance(result.get("work_session"), dict) else None,
        "continuity_reclaim": bool(result.get("continuity_reclaim")),
        "continuity_lease": continuity_lease,
        "evidence_classification": evidence_classification,
        "next_safe_action": result.get("next_safe_action") or "Request mailbox_state for planning or handoff before starting more work.",
    }
    packet: dict[str, Any] = {"state": state, "project": project, "receipt": _compact(receipt), "next_safe_action": receipt["next_safe_action"]}
    if diagnostic:
        packet["_internal"] = {"visibility": "internal", "actual_metadata": actual_metadata, "postcondition_health": health}
    return packet


async def mailbox_close_task(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
) -> dict[str, Any]:
    if dependencies.get is None:
        return _needs_input(
            state,
            project,
            form.id,
            "close_task requires server read access to load the existing task before archiving it.",
            ["server_get_dependency"],
            "Use release_task_claim for lease-only cleanup, or retry through the MCP server.",
        )
    task_id = str(payload["task_id"]).strip()
    if is_short_public_id(task_id):
        async def _unused_get_task_context(_api_base: str, _args: dict[str, Any]) -> dict[str, Any]:
            return {}

        try:
            resolution = await resolve_public_artifact_short_ref(
                api_base=api_base,
                project=project,
                artifact_type="task",
                local_id=task_id,
                dependencies=PublicRefDependencies(
                    get=dependencies.get,
                    get_task_context=_unused_get_task_context,
                    public_error_message=public_mailbox_error_message,
                ),
            )
            task_id = resolution["local_id"]
        except AmbiguousPublicRefError as exc:
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "ambiguous_ref",
                    "form_id": form.id,
                    "message": "Task short id matched multiple artifacts.",
                    "matches": compact_public_ref_matches(exc.matches),
                    "next_safe_action": "Submit close_task again with a longer task_id prefix or full task_id.",
                },
            }
        except Exception as exc:
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "not_found",
                    "form_id": form.id,
                    "message": public_mailbox_error_message(exc),
                    "next_safe_action": "Use get or list open tasks to verify the task id, then submit close_task again.",
                },
            }
    close_status = str(payload.get("close_status") or "obsolete").strip().lower() or "obsolete"
    close_status_aliases = {"done": "completed", "complete": "completed", "finished": "completed"}
    close_status = close_status_aliases.get(close_status, close_status)
    if close_status == "completed":
        receipt = {
            "status": "needs_claim",
            "form_id": form.id,
            "message": "close_task cannot mark completed work because completion requires task ownership proof.",
            "task_id": task_id,
            "requested_close_status": close_status,
            "recommended_next_call": {
                "tool": "submit",
                "form_id": "start_task",
                "payload": {
                    "project": project,
                    "task_id": task_id,
                },
                "why": "Claim/start the task first to obtain work_handle, then submit finish_task with completion evidence.",
            },
            "next_safe_action": "Submit start_task for this task, keep the returned work_handle, then submit finish_task.",
        }
        return {
            "state": state,
            "project": project,
            "receipt": attach_public_diagnostic_incident(
                receipt=receipt,
                kind="completion_requires_owned_claim",
                task_id=task_id,
                recommended_next_call=receipt["recommended_next_call"],
            ),
            "next_safe_action": "Submit start_task for this task, keep the returned work_handle, then submit finish_task.",
        }
    allowed_close_statuses = {"obsolete", "duplicate", "superseded", "cancelled", "not_planned"}
    if close_status not in allowed_close_statuses:
        close_status = "obsolete"
    reason = str(payload["reason"]).strip()
    task = await dependencies.get(api_base, f"/project/tasks/{quote(task_id, safe='')}?project={quote(project, safe='')}")
    existing_tags = _string_list_arg(task.get("tags"))
    extra_tags = _string_list_arg(payload.get("tags"))
    superseded_by = str(payload.get("superseded_by") or "").strip()
    close_tags = [
        "mailbox",
        "task_closed",
        f"close_status:{close_status}",
        *extra_tags,
    ]
    if superseded_by:
        close_tags.append(f"superseded_by:{superseded_by}")
    update_payload = {
        "project": project,
        "task_id": task_id,
        "title": str(task.get("title") or task_id),
        "description": str(task.get("description") or ""),
        "agent_id": mailbox_actor(payload),
        "status": "archived",
        "source": str(task.get("source") or "mailbox_submit.close_task"),
        "tags": _unique_strings([*existing_tags, *close_tags]),
        "topic_path": task.get("topic_path"),
        "linked_improvement_id": task.get("linked_improvement_id"),
    }
    update_payload = {key: value for key, value in update_payload.items() if value not in (None, "", [])}
    result = await dependencies.post(api_base, "/project/tasks", update_payload)
    linked_improvement_sync: dict[str, Any] | None = None
    linked_improvement_id = str(task.get("linked_improvement_id") or "").strip()
    if linked_improvement_id and bool(payload.get("sync_linked_improvement", True)):
        linked_improvement_sync = await _sync_close_task_linked_improvement(
            api_base=api_base,
            project=project,
            task_id=task_id,
            linked_improvement_id=linked_improvement_id,
            close_status=close_status,
            reason=reason,
            acted_by=mailbox_actor(payload),
            dependencies=dependencies,
        )
    change_content = f"Closed task as {close_status}: {reason}"
    if superseded_by:
        change_content += f"\nSuperseded by: {superseded_by}"
    await dependencies.post(
        api_base,
        f"/project/tasks/{quote(task_id, safe='')}/changes",
        {
            "project": project,
            "change_type": "status_change",
            "content": change_content,
            "why": "Task was closed without marking work as completed.",
            "agent_id": mailbox_actor(payload),
            "source": "mailbox_submit.close_task",
            "tags": close_tags,
        },
    )
    release_receipt: dict[str, Any] | None = None
    if bool(payload.get("release_claim", True)):
        release_form = mailbox_form_by_id("release_task_claim") or form
        candidate_release_receipt = (
            await mailbox_release_task_claim(
                form=release_form,
                payload={
                    "project": project,
                    "task_id": task_id,
                    "owner_agent": payload.get("owner_agent") or payload.get("agent_id") or "codex",
                    "session_id": payload.get("session_id") or session_id or "",
                    "reason": f"close_task:{close_status}",
                    "status": "released",
                },
                state=state,
                project=project,
                runtime_profile_id=runtime_profile_id,
                diagnostic=False,
                session_id=session_id,
            )
        ).get("receipt")
        if candidate_release_receipt and candidate_release_receipt.get("status") != "not_found":
            release_receipt = candidate_release_receipt
    result["artifact_key"] = f"task:{project}:{task_id}"
    result["close_status"] = close_status
    actual_metadata = {
        "result_kind": "task_closed",
        "mutation": True,
        "artifact_type": "task",
        "internal_tool": "project_task_archive",
        "route_id": "mailbox.close_task.v1",
    }
    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata=actual_metadata,
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    packet["receipt"]["close_status"] = close_status
    packet["receipt"]["task_status"] = result.get("status") or "archived"
    if linked_improvement_sync:
        packet["receipt"]["linked_improvement_sync"] = linked_improvement_sync
    if superseded_by:
        packet["receipt"]["superseded_by"] = superseded_by
    if release_receipt:
        packet["receipt"]["release"] = release_receipt
    packet["next_safe_action"] = "Request state planning or list open tasks before selecting new work."
    packet["receipt"]["next_safe_action"] = packet["next_safe_action"]
    return packet


async def _sync_close_task_linked_improvement(
    *,
    api_base: str,
    project: str,
    task_id: str,
    linked_improvement_id: str,
    close_status: str,
    reason: str,
    acted_by: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    if dependencies.patch is None:
        return {
            "status": "skipped",
            "reason": "patch_dependency_unavailable",
            "linked_artifact_key": f"improvement:{project}:{linked_improvement_id}",
        }
    try:
        result = await dependencies.patch(
            api_base,
            f"/improvements/{quote(linked_improvement_id, safe='')}/resolve",
            {
                "acted_by": acted_by,
                "action_source": "mailbox_submit.close_task",
                "reason": f"Linked task {task_id} closed as {close_status}: {reason}",
            },
        )
    except Exception as exc:
        return {
            "status": "failed",
            "reason": public_mailbox_error_message(exc),
            "linked_artifact_key": f"improvement:{project}:{linked_improvement_id}",
        }
    return {
        "status": str(result.get("status") or "resolved"),
        "linked_artifact_key": f"improvement:{project}:{linked_improvement_id}",
    }


async def mailbox_create_improvement(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    from app.services.improvements_store import get_improvements_store

    title = str(payload["title"]).strip()
    summary = str(payload["summary"]).strip()
    next_step = str(payload["next_step"]).strip()
    risk = str(payload.get("risk") or "").strip()
    description_parts = [summary, f"Next step: {next_step}"]
    source_project = str(payload.get("source_project") or "").strip()
    if source_project and source_project != project:
        description_parts.append(f"Source project: {source_project}")
    if risk:
        description_parts.append(f"Risk: {risk}")
    evidence_refs = _string_list_arg(payload.get("evidence_refs"))
    if evidence_refs:
        description_parts.append("Evidence refs: " + ", ".join(evidence_refs))
    authority = await _classify_create_improvement_authority(
        api_base=api_base,
        dependencies=dependencies,
        project=project,
        title=title,
        summary=summary,
        next_step=next_step,
    )
    if authority.get("suppress_improvement"):
        receipt = _compact({
            "status": "suppressed",
            "form_id": form.id,
            "mode": form.mode,
            "message": "Proposed improvement duplicates an active project law.",
            **authority,
            "mutation_executed": False,
            "implementation_ready": False,
            "claim_allowed": False,
            "framing_required": False,
            "submitted_fields": sorted(str(key) for key in payload.keys()),
            "next_safe_action": "Reference the matched project law instead of creating a duplicate product improvement.",
        })
        return {
            "state": state,
            "project": project,
            "receipt": receipt,
            "next_safe_action": receipt["next_safe_action"],
            "result": {key: receipt[key] for key in receipt if key not in {"submitted_fields", "next_safe_action"}},
        }
    uid, created = await get_improvements_store().upsert_by_title(
        title=title,
        description="\n".join(description_parts),
        project=project,
        agent_id=mailbox_actor(payload),
        importance_score=float(payload.get("importance_score") or 0.7),
        tags=["mailbox", "mcp-fsm", "mcp-improvement"],
    )
    row = await get_improvements_store().get(uid)
    description = str(row.get("description") if row else "\n".join(description_parts))
    task_id = str(uid)
    task_payload = {
        "project": project,
        "task_id": task_id,
        "title": str(row.get("title") if row else title),
        "description": description,
        "agent_id": mailbox_actor(payload),
        "status": "planning",
        "source": "improvement",
        "tags": [
            "mailbox",
            "mcp-fsm",
            "mcp-improvement",
            "improvement_projection",
            "framing_required",
            "entity:task",
            f"task_id:{task_id}",
            "task_status:planning",
        ],
        "linked_improvement_id": task_id,
    }
    if source_project and source_project != project:
        task_payload["tags"].append(f"source_project:{source_project}")
    task_result = await dependencies.post(api_base, "/project/tasks", task_payload)
    canonical_task_id = str(task_result.get("task_id") or task_id)
    task_artifact_key = f"task:{project}:{canonical_task_id}"
    canonical_task_status = str(task_result.get("status") or "planning")
    await dependencies.post(
        api_base,
        f"/project/tasks/{quote(task_id, safe='')}/changes",
        {
            "project": project,
            "change_type": "task_created",
            "content": f"Task bootstrapped from mailbox improvement '{task_payload['title']}'.",
            "why": (
                "Public create_improvement keeps a compatibility task projection, but the improvement "
                "must be deliberately framed and approved before implementation."
            ),
            "agent_id": mailbox_actor(payload),
            "source": "mailbox_submit.create_improvement",
            "tags": [
                "mailbox",
                "mcp-fsm",
                "mcp-improvement",
                *([f"source_project:{source_project}"] if source_project and source_project != project else []),
            ],
        },
    )
    result = {
        "id": str(uid),
        "artifact_key": f"improvement:{project}:{uid}",
        **authority,
        "created": bool(created),
        "created_task": bool(created),
        "idempotent_reuse": not bool(created),
        "title": task_payload["title"],
        "task_id": canonical_task_id,
        "canonical_task_id": canonical_task_id,
        "task_artifact_key": task_artifact_key,
        "linked_artifact_key": task_artifact_key,
        "task_status": canonical_task_status,
        "canonical_status": canonical_task_status,
        "lifecycle_stage": "proposal",
        "implementation_ready": False,
        "claim_allowed": False,
        "framing_required": True,
    }
    actual_metadata = {
        "result_kind": "artifact_created",
        "artifact_type": "improvement",
        "mutation": True,
        "review_mode": False,
        "internal_tool": "improvements_store.upsert_by_title+project_task_bootstrap",
        "route_id": "mailbox.create_improvement.v1",
    }
    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata=actual_metadata,
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    packet["receipt"]["authority_layer"] = result.get("authority_layer")
    packet["receipt"]["classification_reason"] = result.get("classification_reason")
    packet["receipt"]["matched_law_ref"] = result.get("matched_law_ref")
    packet["receipt"]["matched_law_title"] = result.get("matched_law_title")
    packet["receipt"]["matched_law_status"] = result.get("matched_law_status")
    packet["receipt"]["task_id"] = canonical_task_id
    packet["receipt"]["canonical_task_id"] = canonical_task_id
    packet["receipt"]["task_artifact_key"] = task_artifact_key
    packet["receipt"]["linked_artifact_key"] = result["linked_artifact_key"]
    packet["receipt"]["task_status"] = result["task_status"]
    packet["receipt"]["canonical_status"] = result["canonical_status"]
    packet["receipt"]["created_task"] = result["created_task"]
    packet["receipt"]["idempotent_reuse"] = result["idempotent_reuse"]
    packet["receipt"]["lifecycle_stage"] = result["lifecycle_stage"]
    packet["receipt"]["implementation_ready"] = False
    packet["receipt"]["claim_allowed"] = False
    packet["receipt"]["framing_required"] = True
    packet["receipt"]["next_safe_action"] = (
        "Keep this as an improvement and return to the current task. When selected by priority, "
        "review it and complete task framing before requesting implementation approval."
    )
    packet["next_safe_action"] = packet["receipt"]["next_safe_action"]
    return packet


async def _classify_create_improvement_authority(
    *,
    api_base: str,
    dependencies: MailboxActionDependencies,
    project: str,
    title: str,
    summary: str,
    next_step: str,
) -> dict[str, Any]:
    laws: list[dict[str, Any]] = []
    if dependencies.get is not None:
        try:
            data = await dependencies.get(
                api_base,
                f"/laws?project={quote(project, safe='')}&status=active&include_promoted=true&limit=100",
            )
            if isinstance(data, dict):
                laws = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        except Exception:
            laws = []
    return _compact(classify_improvement_authority(
        project=project,
        title=title,
        summary=summary,
        next_step=next_step,
        laws=laws,
    ).public_dict())


async def mailbox_store_memory(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    tags = ["mailbox", "stored_fact", *_string_list_arg(payload.get("tags"))]
    memory_payload = {
        "content": str(payload["content"]).strip(),
        "memory_type": str(payload.get("memory_type") or "context").strip() or "context",
        "category": str(payload.get("category") or "mnemoforge:fact").strip() or "mnemoforge:fact",
        "project": project,
        "agent_id": mailbox_actor(payload),
        "importance_score": float(payload.get("importance_score") or 0.6),
        "tags": tags,
        "source": str(payload.get("source") or "mailbox_submit.store_memory").strip() or "mailbox_submit.store_memory",
    }
    result = await dependencies.post(api_base, "/memories", memory_payload)
    if result.get("id") and not result.get("artifact_key"):
        result["artifact_key"] = f"memory:{project}:{result['id']}"
    actual_metadata = {
        "result_kind": "memory_stored",
        "mutation": True,
        "artifact_type": "memory",
        "internal_tool": "memory_store",
        "route_id": "mailbox.store_memory.v1",
    }
    return build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata=actual_metadata,
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )


async def mailbox_create_law(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    requested_status = str(payload.get("status") or "").strip()
    confirmed_by = str(payload.get("confirmed_by") or "").strip()
    status = requested_status or ("user_confirmed" if confirmed_by else "proposed")
    if status in {"active", "user_confirmed"} and not confirmed_by:
        return _needs_input(
            state,
            project,
            form.id,
            "confirmed_by is required before a law can become active or user_confirmed.",
            ["confirmed_by"],
            "Submit create_law with confirmed_by, or use status=proposed and confirm_law later.",
        )
    law_payload = {
        "project": project,
        "title": str(payload["title"]).strip(),
        "statement": str(payload["statement"]).strip(),
        "rationale": str(payload.get("rationale") or "").strip(),
        "evidence": _string_list_arg(payload.get("evidence")),
        "agent_id": mailbox_actor(payload),
        "scope": str(payload.get("target_scope") or payload.get("scope") or "project").strip() or "project",
        "status": status,
        "confirmed_by": confirmed_by or None,
        "confirmation_source": str(payload.get("confirmation_source") or "mailbox_submit.create_law").strip()
        if confirmed_by
        else None,
        "tags": _string_list_arg(payload.get("tags")) or ["mailbox", "project_law"],
        "topic_path": str(payload.get("topic_path") or "").strip() or None,
    }
    law_payload = {key: value for key, value in law_payload.items() if value not in (None, "", [])}
    result = await dependencies.post(api_base, "/laws", law_payload)
    if result.get("id") and not result.get("artifact_key"):
        result["artifact_key"] = f"law:{project}:{result['id']}"
    actual_metadata = {
        "result_kind": "law_created",
        "mutation": True,
        "artifact_type": "project_law",
        "internal_tool": "create_project_law",
        "route_id": "mailbox.create_law.v1",
    }
    return build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata=actual_metadata,
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )


async def mailbox_confirm_law(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    law_id = str(payload["law_id"]).strip()
    confirm_payload = {
        "confirmed_by": str(payload["confirmed_by"]).strip(),
        "confirmation_source": str(payload.get("confirmation_source") or "mailbox_submit.confirm_law").strip(),
        "reason": str(payload.get("reason") or "").strip(),
        "activate": bool(payload.get("activate", True)),
    }
    result = await dependencies.post(api_base, f"/laws/{quote(law_id, safe='')}/confirm", confirm_payload)
    if result.get("id") and not result.get("artifact_key"):
        result["artifact_key"] = f"law:{project}:{result['id']}"
    actual_metadata = {
        "result_kind": "law_confirmed",
        "mutation": True,
        "artifact_type": "project_law",
        "internal_tool": "confirm_project_law",
        "route_id": "mailbox.confirm_law.v1",
    }
    return build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata=actual_metadata,
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )


def mailbox_set_feature_gate(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
) -> dict[str, Any]:
    from app.services.mcp_feature_gates import get_mcp_feature_gate_store

    scope = str(payload.get("scope") or "session").strip().lower()
    scope_id = str(payload.get("scope_id") or "").strip()
    if not scope_id:
        scope_id = {
            "project": project,
            "runtime_profile": runtime_profile_id,
            "global": "global",
        }.get(scope, str(payload.get("agent_fingerprint") or payload.get("agent_id") or "default").strip() or "default")
    gate = get_mcp_feature_gate_store().set_gate(
        feature_id=str(payload["feature_id"]).strip(),
        scope=scope,
        scope_id=scope_id,
        enabled=bool(payload["enabled"]),
        reason=str(payload.get("reason") or "mailbox_submit"),
        updated_by=mailbox_actor(payload),
    )
    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata={
            "result_kind": "feature_gate_updated",
            "mutation": True,
            "internal_tool": "mcp_feature_gate_store.set_gate",
            "route_id": "mailbox.set_feature_gate.v1",
        },
        result={"id": f"{gate['feature_id']}:{gate['scope']}:{gate['scope_id']}", "artifact_key": f"feature_gate:{gate['scope']}:{gate['scope_id']}:{gate['feature_id']}"},
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    packet["receipt"].update({"feature_id": gate["feature_id"], "scope": gate["scope"], "scope_id": gate["scope_id"], "enabled": gate["enabled"]})
    return packet


def mailbox_route_hygiene(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
) -> dict[str, Any]:
    facade = str(payload.get("facade") or "").strip()
    limit = max(1, min(int(payload.get("limit") or 50), 200))
    stale_after_days = max(1, min(int(payload.get("stale_after_days") or 30), 365))
    report = get_route_pattern_store().hygiene_report(
        facade=facade,
        known_tools=_known_route_tools(),
        limit=limit,
        stale_after_days=stale_after_days,
    )
    if facade:
        report["patterns"] = [
            item for item in report.get("patterns", [])
            if str(item.get("facade") or "") == facade
        ]
        report["disabled_patterns"] = [
            item for item in report.get("disabled_patterns", [])
            if str(item.get("facade") or "") == facade
        ]
        report["findings"] = [
            item for item in report.get("findings", [])
            if str(item.get("facade") or "") == facade
        ]
        report["summary"] = {
            **(report.get("summary") if isinstance(report.get("summary"), dict) else {}),
            "facade_filter": facade,
            "returned_patterns": len(report["patterns"]),
            "returned_disabled_patterns": len(report["disabled_patterns"]),
            "returned_findings": len(report["findings"]),
        }
    if not bool(payload.get("include_disabled", True)):
        report.pop("disabled_patterns", None)

    return {
        "state": state,
        "project": project,
        "receipt": {
            "status": "accepted",
            "form_id": form.id,
            "mode": form.mode,
            "message": "Route hygiene report generated.",
            "next_safe_action": "Use route_feedback to reinforce useful phrases or invalidate stale learned routes.",
        },
        "result": report,
        "next_safe_action": "Use route_feedback to reinforce useful phrases or invalidate stale learned routes.",
    }


def mailbox_diagnostic_inspection(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
) -> dict[str, Any]:
    result = build_diagnostic_inspection_packet(
        project=project,
        payload=payload,
        diagnostic=diagnostic,
    )
    return {
        "state": state,
        "project": project,
        "receipt": {
            "status": "accepted",
            "form_id": form.id,
            "mode": form.mode,
            "message": "Diagnostic inspection packet generated.",
            "next_safe_action": result.get("next_diagnostic_action", "Continue normal workflow."),
        },
        "result": result,
        "next_safe_action": result.get("next_diagnostic_action", "Continue normal workflow."),
    }


def mailbox_developer_feedback_packet(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
) -> dict[str, Any]:
    result = build_developer_feedback_packet(
        project=project,
        payload=payload,
        diagnostic=diagnostic,
    )
    return {
        "state": state,
        "project": project,
        "receipt": {
            "status": result.get("status", "ready"),
            "form_id": form.id,
            "mode": form.mode,
            "message": "Developer feedback packet generated.",
            "next_safe_action": result.get("next_safe_action", "Review the packet before sending it to maintainers."),
        },
        "result": result,
        "next_safe_action": result.get("next_safe_action", "Review the packet before sending it to maintainers."),
    }


async def mailbox_knowledge_refinement_feedback(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
) -> dict[str, Any]:
    request = build_knowledge_refinement_request(
        project=project,
        payload=payload,
        actor=mailbox_actor(payload),
    )
    result = await build_knowledge_refinement_packet(
        request=request,
        api_base=api_base,
        get=dependencies.get,
        patch=dependencies.patch,
        post=dependencies.post,
    )
    if result.get("status") == "needs_input":
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "needs_input",
                "form_id": form.id,
                "target_ref": result.get("target_ref"),
                "target_type": result.get("target_type"),
                "refinement_type": result.get("refinement_type"),
                "message": result.get("message", "Knowledge refinement needs more input."),
                "next_safe_action": result.get("next_safe_action", "Review the refinement request and resubmit."),
            },
        }

    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata={
            "result_kind": "knowledge_refinement_feedback",
            "mutation": bool(result.get("mutation_executed", False)),
            "target_type": result.get("target_type"),
            "refinement_type": result.get("refinement_type"),
            "internal_tool": "knowledge_refinement_service",
            "route_id": "mailbox.knowledge_refinement_feedback.v1",
        },
        result={
            "id": str(result.get("target_ref") or request.target_ref),
            "artifact_key": str(result.get("target_ref") or request.target_ref),
        },
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    packet["result"] = result
    packet["receipt"].update(
        _compact(
            {
                "target_ref": result.get("target_ref") or request.target_ref,
                "target_type": result.get("target_type") or request.target_type,
                "refinement_type": result.get("refinement_type") or request.refinement_type,
                "refinement_status": result.get("status"),
                "mutation_executed": bool(result.get("mutation_executed", False)),
                "message": _knowledge_refinement_receipt_message(result),
                "postcondition_satisfied": (result.get("lifecycle") or {}).get("postcondition", {}).get("satisfied"),
            }
        )
    )
    packet["next_safe_action"] = str(result.get("next_safe_action") or packet["next_safe_action"])
    packet["receipt"]["next_safe_action"] = packet["next_safe_action"]
    return packet


def _knowledge_refinement_receipt_message(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip()
    if status == "preview":
        return "Knowledge refinement preview built; no mutation was executed."
    if status == "preview_unsupported":
        return "Knowledge refinement preview found this target/refinement combination is not safely applicable."
    if status == "blocked_static_spec":
        return "Static spec refinement was not applied at runtime; development work is recommended."
    if status == "applied":
        return "Knowledge refinement was applied through a governed live-DB service."
    if status == "unsupported_target":
        return "Target type is not yet supported by the universal refinement contour."
    return "Knowledge refinement request was processed."


def mailbox_route_feedback(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
) -> dict[str, Any]:
    facade = str(payload.get("facade") or "").strip()
    pattern_id = str(payload.get("pattern_id") or "").strip()
    query = str(payload.get("query") or "").strip()
    vote = str(payload.get("vote") or "negative").strip().lower()
    if vote not in {"positive", "negative"}:
        vote = "negative"
    if not pattern_id and not query:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "needs_input",
                "form_id": form.id,
                "message": "route_feedback requires pattern_id or query.",
                "missing_fields": ["pattern_id_or_query"],
                "next_safe_action": "Submit route_feedback with a learned pattern_id, or facade plus the misrouted query.",
            },
        }

    matched_route: dict[str, Any] | None = None
    if not pattern_id:
        matched_route = get_route_pattern_store().match(facade=facade, pattern=query)
        pattern_id = str((matched_route or {}).get("pattern_id") or "").strip()
    if not pattern_id:
        expected_tool = str(payload.get("expected_tool") or "").strip()
        expected_intent_type = str(payload.get("expected_intent_type") or "").strip()
        learned_payload = _route_feedback_expected_payload(
            facade=facade,
            intent_type=expected_intent_type,
            payload=payload,
        )
        if vote == "positive" and query and expected_tool and expected_intent_type:
            pattern_id = get_route_pattern_store().record(
                facade=facade,
                pattern=query,
                intent_type=expected_intent_type,
                tool=expected_tool,
                mutating=bool(payload.get("mutating", False)),
                confidence=float(payload.get("confidence") or 0.65),
                source="operator_feedback",
                metadata={
                    "reason": str(payload.get("reason") or "operator_positive_alias").strip(),
                    "matched_example": query,
                    "alias_source": "route_feedback",
                    "learned_payload": learned_payload,
                },
            )
        if pattern_id:
            matched_route = {
                "pattern_id": pattern_id,
                "tool": expected_tool,
                "intent_type": expected_intent_type,
            }
    if not pattern_id:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "not_found",
                "form_id": form.id,
                "message": "No active learned route pattern matched the feedback.",
                "next_safe_action": "Request route_hygiene in operator_review, or submit positive feedback with query, expected_tool, and expected_intent_type to create a learned alias.",
            },
        }

    disable = bool(payload.get("disable", True))
    metadata = _compact(
        {
            "project": project,
            "facade": facade,
            "query": query,
            "vote": vote,
            "language": str(payload.get("language") or "").strip(),
            "phrase_family": str(payload.get("phrase_family") or "").strip(),
            "jargon_terms": _string_list_arg(payload.get("jargon_terms")),
            "typo_terms": _string_list_arg(payload.get("typo_terms")),
            "keyboard_layout_terms": _string_list_arg(payload.get("keyboard_layout_terms")),
            "expected_tool": str(payload.get("expected_tool") or "").strip(),
            "expected_intent_type": str(payload.get("expected_intent_type") or "").strip(),
            "expected_payload": _route_feedback_expected_payload(
                facade=facade,
                intent_type=str(payload.get("expected_intent_type") or (matched_route or {}).get("intent_type") or "").strip(),
                payload=payload,
            ),
            "actual_tool": str(payload.get("actual_tool") or (matched_route or {}).get("tool") or "").strip(),
            "actual_intent_type": str(payload.get("actual_intent_type") or (matched_route or {}).get("intent_type") or "").strip(),
            "updated_by": mailbox_actor(payload),
        }
    )
    feedback = get_route_pattern_store().record_feedback(
        pattern_id,
        vote=vote,
        reason=str(payload.get("reason") or "").strip(),
        metadata=metadata,
    )
    if feedback is None:
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "not_found",
                "form_id": form.id,
                "pattern_id": pattern_id,
                "message": "Learned route pattern does not exist.",
                "next_safe_action": "Request route_hygiene in operator_review or retry with the exact misrouted query.",
            },
        }

    disabled = False
    disable = bool(payload.get("disable", True)) if vote == "negative" else False
    if disable:
        disabled = get_route_pattern_store().disable_pattern(
            pattern_id,
            reason=str(payload.get("reason") or "operator_negative_feedback").strip() or "operator_negative_feedback",
            metadata=metadata,
        )
        if not disabled:
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "not_found",
                    "form_id": form.id,
                    "pattern_id": pattern_id,
                    "message": "Learned route pattern was already disabled or does not exist.",
                    "next_safe_action": "Request diagnostic state or retry with a current active pattern_id.",
                },
            }

    packet = build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata={
            "result_kind": "route_feedback_recorded",
            "mutation": True,
            "internal_tool": "route_pattern_store.record_feedback",
            "route_id": "mailbox.route_feedback.v1",
        },
        result={"id": pattern_id, "artifact_key": f"route_pattern:{facade}:{pattern_id}"},
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )
    target_ref = f"route_pattern:{facade}:{pattern_id}"
    action = "reinforce" if vote == "positive" else "disable" if disabled else "quarantine"
    lifecycle = build_refinement_lifecycle(
        project=project,
        payload={**payload, "apply": True},
        target_ref=target_ref,
        target_type="route_pattern",
        action=action,
        actor=mailbox_actor(payload),
        adapter="route_pattern_feedback",
        default_observed=str(payload.get("reason") or "").strip(),
        default_expected=(
            "The learned route remains active with reinforced evidence."
            if vote == "positive"
            else "The incorrect learned route no longer influences routing."
        ),
    )
    feedback_snapshot = feedback if isinstance(feedback, dict) else {}
    expected_postcondition = {
        "feedback_recorded": True,
        "active": not disabled,
    }
    actual_postcondition = {
        "feedback_recorded": bool(feedback_snapshot),
        "active": not disabled,
        "positive_feedback": feedback_snapshot.get("positive_feedback"),
        "negative_feedback": feedback_snapshot.get("negative_feedback"),
    }
    lifecycle = complete_refinement_lifecycle(
        lifecycle,
        status="applied",
        mutation_executed=True,
        postcondition_expected=expected_postcondition,
        postcondition_actual=actual_postcondition,
        postcondition_satisfied=bool(feedback_snapshot),
        audit_evidence=[target_ref, *_string_list_arg(payload.get("evidence_refs"))],
        reversible=False,
        reversal_action="Submit a reviewed positive route refinement or recreate the alias through route feedback.",
    )
    packet["result"] = {
        "target_ref": target_ref,
        "target_type": "route_pattern",
        "refinement_type": action,
        "mutation_executed": True,
        "feedback": feedback_snapshot,
        "lifecycle": lifecycle,
    }
    packet["receipt"].update(
        {
            "pattern_id": pattern_id,
            "feedback_action": "disabled" if disabled else f"{vote}_recorded",
            "facade": facade,
            "vote": vote,
            "target_ref": target_ref,
            "refinement_type": action,
            "postcondition_satisfied": lifecycle["postcondition"]["satisfied"],
        }
    )
    if vote == "positive":
        packet["next_safe_action"] = "The learned route pattern was reinforced; keep using this phrasing when it matches the desired route."
    elif disabled:
        packet["next_safe_action"] = "Retry the original user request; the stale learned route pattern is no longer active."
        packet["receipt"] = attach_public_diagnostic_incident(
            receipt=packet["receipt"],
            kind="stale_learned_route",
            safe_next_action=packet["next_safe_action"],
            recommended_next_call={
                "tool": facade or "ask_project",
                "arguments": _compact({
                    "project": project,
                    "question": query if facade == "ask_project" else "",
                    "intent": query if facade != "ask_project" else "",
                    "response_format": "diagnostic",
                }),
            },
        )
    else:
        packet["next_safe_action"] = "Negative feedback was recorded; use route_hygiene to decide whether to disable this pattern."
        packet["receipt"] = attach_public_diagnostic_incident(
            receipt=packet["receipt"],
            kind="route_misclassification",
            safe_next_action=packet["next_safe_action"],
            recommended_next_call={
                "tool": "submit",
                "form_id": "route_feedback",
                "payload": _compact({
                    "project": project,
                    "facade": facade,
                    "pattern_id": pattern_id,
                    "vote": "negative",
                    "disable": True,
                }),
            },
        )
    packet["receipt"]["next_safe_action"] = packet["next_safe_action"]
    return packet


def _route_feedback_expected_payload(*, facade: str, intent_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw_payload = payload.get("expected_payload")
    if not isinstance(raw_payload, dict):
        return {}
    try:
        spec = load_named_json_spec("learning/route_parameters.json")
    except Exception:
        return {}
    allowed_by_facade = spec.get("allowed_payload_fields")
    if not isinstance(allowed_by_facade, dict):
        return {}
    allowed_by_intent = allowed_by_facade.get(str(facade or "").strip())
    if not isinstance(allowed_by_intent, dict):
        return {}
    allowed_fields = allowed_by_intent.get(str(intent_type or "").strip())
    if not isinstance(allowed_fields, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for field, allowed_values in allowed_fields.items():
        value = raw_payload.get(field)
        if value in (None, "", [], {}):
            continue
        if isinstance(allowed_values, list):
            allowed = {str(item) for item in allowed_values}
            if str(value) not in allowed:
                continue
        sanitized[str(field)] = value
    return sanitized


def _known_route_tools() -> set[str]:
    tools: set[str] = set()
    for facade in ("project_work", "project_rules", "project_context", "project_verify", "project_capture"):
        try:
            catalog = load_route_catalog_spec(facade)
        except Exception:
            continue
        tools.update(str(route.tool or "").strip() for route in catalog.routes if str(route.tool or "").strip())
    tools.update(
        {
            "ask_project",
            "project_work",
            "project_rules",
            "project_context",
            "project_verify",
            "project_capture",
            "get",
            "submit",
            "state",
            "help",
            "mailbox_state",
            "mailbox_submit",
            "mailbox_get",
        }
    )
    return tools


async def mailbox_record_progress(
    *,
    form,
    payload: dict[str, Any],
    state: str,
    project: str,
    runtime_profile_id: str,
    diagnostic: bool,
    api_base: str,
    dependencies: MailboxActionDependencies,
    session_id: str | None,
) -> dict[str, Any]:
    task_id = str(payload.get("task_id") or "").strip()
    stage = str(payload.get("stage") or "in_progress").strip().lower() or "in_progress"
    evidence_classification = classify_evidence_items(_string_list_arg(payload.get("verification")))
    if task_id:
        lease_guard = dependencies.task_mutation_guard(
            project=project,
            task_id=task_id,
            owner_agent=mailbox_actor(payload),
            owner_session_id=str(payload.get("session_id") or session_id or ""),
            tool_name="mailbox_submit.record_progress",
            work_token=str(payload.get("work_token") or ""),
            work_handle=str(payload.get("work_handle") or ""),
            danger_mode=bool(payload.get("danger_mode", False)),
            danger_confirmation=str(payload.get("danger_confirmation") or ""),
        )
        if lease_guard:
            reclaim_call = _record_progress_reclaim_call(
                payload=payload,
                project=project,
                task_id=task_id,
                lease_guard=lease_guard,
            )
            receipt = {
                "status": "conflict",
                "form_id": form.id,
                "message": "Task progress requires an active owned claim when task_id is provided.",
                "recommended_reclaim_call": reclaim_call,
                "next_safe_action": lease_guard.get("next_safe_action", "Claim the task before recording task progress."),
            }
            return {
                "state": state,
                "project": project,
                "receipt": attach_public_diagnostic_incident(
                    receipt=receipt,
                    kind="work_started_without_claim_or_missing_token",
                    task_id=task_id,
                    recommended_next_call=reclaim_call,
                ),
            }
        checkpoint_args = {
            "project": project,
            "task_id": task_id,
            "stage": stage,
            "summary": str(payload["summary"]).strip(),
            "changed_files": _string_list_arg(payload.get("changed_files")),
            "verification": _string_list_arg(payload.get("verification")),
            "next_step": str(payload.get("next_step") or "").strip(),
            "status": str(payload.get("status") or "active").strip() or "active",
            "reason": str(payload.get("reason") or "mailbox_record_progress").strip(),
            "acted_by": mailbox_actor(payload),
            "source": "mailbox_submit.record_progress",
            "checkpoint_mode": "lightweight",
        }
        checkpoint_payload = build_report_task_checkpoint_payload(checkpoint_args)
        result = await dependencies.post(api_base, f"/project/tasks/{quote(task_id, safe='')}/changes", checkpoint_payload)
        if result.get("id"):
            result["artifact_key"] = f"task:{project}:{task_id}"
            result["stage"] = stage
            result["evidence_classification"] = evidence_classification
        _record_closeout_spans_from_progress(
            payload=payload,
            project=project,
            task_id=task_id,
            owner_agent=mailbox_actor(payload),
            owner_session_id=str(payload.get("session_id") or session_id or ""),
        )
        actual_metadata = {
            "result_kind": "progress_recorded",
            "mutation": True,
            "artifact_type": "task_checkpoint",
            "internal_tool": "project_task_change",
            "route_id": "mailbox.record_progress.task_checkpoint.v1",
        }
        return build_mailbox_mutation_packet(
            form=form,
            payload=payload,
            state=state,
            project=project,
            actual_metadata=actual_metadata,
            result=result,
            runtime_profile_id=runtime_profile_id,
            diagnostic=diagnostic,
        )

    memory_payload = {
        "content": str(payload["summary"]).strip(),
        "memory_type": "context",
        "category": "mnemoforge:progress",
        "project": project,
        "agent_id": mailbox_actor(payload),
        "importance_score": 0.5,
        "tags": ["mailbox", "progress", f"stage:{stage}"],
        "source": "mailbox_submit.record_progress",
    }
    result = await dependencies.post(api_base, "/memories", memory_payload)
    result["stage"] = stage
    result["evidence_classification"] = evidence_classification
    actual_metadata = {
        "result_kind": "progress_recorded",
        "mutation": True,
        "artifact_type": "memory",
        "internal_tool": "memory_store",
        "route_id": "mailbox.record_progress.memory.v1",
    }
    return build_mailbox_mutation_packet(
        form=form,
        payload=payload,
        state=state,
        project=project,
        actual_metadata=actual_metadata,
        result=result,
        runtime_profile_id=runtime_profile_id,
        diagnostic=diagnostic,
    )


def mailbox_actor(payload: dict[str, Any]) -> str:
    return str(payload.get("updated_by") or payload.get("agent_id") or payload.get("owner_agent") or "codex").strip() or "codex"


def _generated_mailbox_session_id(payload: dict[str, Any]) -> str:
    owner = mailbox_actor(payload)
    fingerprint = str(payload.get("agent_fingerprint") or "").strip()
    if fingerprint:
        return f"mailbox-auto-{owner}-{fingerprint}"[:120]
    return f"mailbox-auto-{uuid.uuid4().hex[:12]}"


def _start_task_reclaim_payload(*, result: dict[str, Any], previous_lease: dict[str, Any]) -> dict[str, Any]:
    if not result.get("same_fingerprint_reclaim"):
        return {}
    previous_status = str(previous_lease.get("status") or "").strip()
    reason = "previous_lease_expired" if previous_status == "expired" else "same_fingerprint_reclaim"
    return _compact(
        {
            "same_fingerprint": True,
            "reason": reason,
            "previous_lease_id": previous_lease.get("lease_id"),
            "previous_status": previous_status,
        }
    )


def _start_task_conflict_packet(
    *,
    state: str,
    project: str,
    form_id: str,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    receipt = _start_task_conflict_receipt(
        form_id=form_id,
        payload=payload,
        project=project,
        result=result,
    )
    return {
        "state": state,
        "project": project,
        "receipt": _compact(receipt),
        "next_safe_action": receipt["next_safe_action"],
    }


def _start_task_conflict_receipt(
    *,
    form_id: str,
    payload: dict[str, Any],
    project: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    active_lease = public_lease_payload(error.get("active_lease") if isinstance(error.get("active_lease"), dict) else None)
    payload_fingerprint = str(payload.get("agent_fingerprint") or "").strip()
    active_fingerprint = str(active_lease.get("agent_fingerprint") or "").strip()
    same_fingerprint = bool(payload_fingerprint and active_fingerprint and payload_fingerprint == active_fingerprint)
    message = _start_task_conflict_message(result=result, same_fingerprint=same_fingerprint)
    receipt: dict[str, Any] = {
        "status": result.get("status") or "conflict",
        "form_id": form_id,
        "message": message,
        "lease": active_lease,
        "same_fingerprint": same_fingerprint,
        "expires_at": active_lease.get("expires_at") or result.get("expires_at"),
        "next_safe_action": _start_task_conflict_next_action(
            result=result,
            same_fingerprint=same_fingerprint,
            has_work_token=bool(str(payload.get("work_token") or "").strip()),
        ),
    }
    recovery = _start_task_recovery_options(
        project=project,
        payload=payload,
        active_lease=active_lease,
        same_fingerprint=same_fingerprint,
    )
    if recovery:
        receipt["recovery_options"] = recovery
    reclaim_call = _start_task_recommended_reclaim_call(
        project=project,
        payload=payload,
        same_fingerprint=same_fingerprint,
    )
    if reclaim_call:
        receipt["recommended_reclaim_call"] = reclaim_call
    return receipt


def _start_task_conflict_message(*, result: dict[str, Any], same_fingerprint: bool) -> str:
    if result.get("error") == "work_token_invalid":
        return "work_token did not match the active same-fingerprint claim."
    if same_fingerprint:
        return "Task is already claimed by this same agent fingerprint."
    if isinstance(result.get("error"), dict):
        return "Task is already claimed by another active session."
    return str(result.get("message") or result.get("error") or "start_task_session did not start.")


def _start_task_conflict_next_action(
    *,
    result: dict[str, Any],
    same_fingerprint: bool,
    has_work_token: bool,
) -> str:
    if result.get("error") == "work_token_invalid":
        return "Do not reclaim this task; recover the correct work_token from the previous start_task receipt or wait for lease expiry."
    if same_fingerprint and has_work_token:
        return "Submit recommended_reclaim_call to recover the same active work session."
    if same_fingerprint:
        return "Recover work_token from the previous start_task receipt and submit start_task with the same task_id, agent_fingerprint, and work_token; otherwise wait for lease expiry."
    return result.get("next_safe_action") or "Do not start this task; coordinate with the current owner or wait for lease expiry."


def _start_task_recovery_options(
    *,
    project: str,
    payload: dict[str, Any],
    active_lease: dict[str, Any],
    same_fingerprint: bool,
) -> list[dict[str, Any]]:
    task_id = str(payload.get("task_id") or active_lease.get("task_id") or "").strip()
    if not task_id:
        return []
    options: list[dict[str, Any]] = [
        {
            "id": "inspect_task",
            "tool": "get",
            "ref": f"task:{project}:{task_id}",
            "why": "Read current task context before retrying a claim.",
        }
    ]
    if same_fingerprint:
        options.append(
            {
                "id": "same_fingerprint_reclaim",
                "tool": "submit",
                "form_id": "start_task",
                "requires": ["work_token"],
                "why": "Use when this is your crashed/lost previous session and you still have the work_token.",
            }
        )
    else:
        options.append(
            {
                "id": "wait_or_coordinate",
                "expires_at": active_lease.get("expires_at"),
                "why": "Another active owner holds the lease; wait for expiry or coordinate handoff.",
            }
        )
    return options


def _start_task_recommended_reclaim_call(
    *,
    project: str,
    payload: dict[str, Any],
    same_fingerprint: bool,
) -> dict[str, Any]:
    work_token = str(payload.get("work_token") or "").strip()
    if not same_fingerprint or not work_token:
        return {}
    reclaim_payload = _compact(
        {
            "project": project,
            "task_id": payload.get("task_id"),
            "owner_agent": payload.get("owner_agent") or payload.get("agent_id"),
            "agent_fingerprint": payload.get("agent_fingerprint"),
            "work_token": work_token,
            "approved_framing": payload.get("approved_framing"),
            "framing_version": payload.get("framing_version"),
            "approval_intent": payload.get("approval_intent"),
            "runtime_profile_id": payload.get("runtime_profile_id"),
            "lease_ttl_seconds": payload.get("lease_ttl_seconds"),
        }
    )
    return {
        "tool": "submit",
        "form_id": "start_task",
        "state": "planning",
        "project": project,
        "payload": reclaim_payload,
    }


def _record_progress_reclaim_call(
    *,
    payload: dict[str, Any],
    project: str,
    task_id: str,
    lease_guard: dict[str, Any],
) -> dict[str, Any]:
    if lease_guard.get("error") != "active_claim_required":
        return {}
    reclaim_payload = _compact(
        {
            "project": project,
            "task_id": task_id,
            "owner_agent": payload.get("owner_agent") or payload.get("agent_id"),
            "agent_fingerprint": payload.get("agent_fingerprint"),
        }
    )
    return {
        "tool": "submit",
        "form_id": "start_task",
        "state": "planning",
        "project": project,
        "payload": reclaim_payload,
    }


def _record_closeout_spans_from_progress(
    *,
    payload: dict[str, Any],
    project: str,
    task_id: str,
    owner_agent: str,
    owner_session_id: str,
) -> None:
    from app.services.stenographer_service import get_stenographer_store

    store = get_stenographer_store()
    active_work = None
    if owner_session_id:
        active_work = store.get_active_work_by_task(
            project=project,
            task_id=task_id,
            agent_id=owner_agent,
            session_id=owner_session_id,
        )
    if active_work is None and str(payload.get("work_token") or "").strip():
        active_work = store.get_active_work_by_task_any_session(
            project=project,
            task_id=task_id,
            agent_id=owner_agent,
        )
    if active_work is None:
        return
    span_session_id = active_work.session_id or owner_session_id
    for kind, values in (
        ("verification", _string_list_arg(payload.get("verification"))),
        ("changed_files", _closeout_span_values(payload, "changed_files")),
        ("next_step", _string_list_arg(payload.get("next_step"))),
    ):
        for value in values:
            store.record_span(
                project=project,
                task_id=task_id,
                work_id=active_work.work_id,
                agent_id=owner_agent,
                session_id=span_session_id,
                kind=kind,
                source="mailbox_submit.record_progress",
                content=value,
            )


def _string_list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _closeout_span_values(payload: dict[str, Any], key: str) -> list[str]:
    values = _string_list_arg(payload.get(key))
    if values:
        return values
    if key == "changed_files" and key in payload:
        return ["none"]
    return []


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _compact(receipt: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in receipt.items() if value not in (None, "", [])}


def _needs_input(
    state: str,
    project: str,
    form_id: str,
    message: str,
    missing_fields: list[str],
    next_safe_action: str,
) -> dict[str, Any]:
    return {
        "state": state,
        "project": project,
        "receipt": {
            "status": "needs_input",
            "form_id": form_id,
            "message": message,
            "missing_fields": missing_fields,
            "next_safe_action": next_safe_action,
        },
    }
