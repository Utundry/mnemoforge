from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.mcp_mailbox import (
    build_mailbox_mutation_packet,
    build_mailbox_submit_receipt,
    evaluate_mailbox_postconditions,
    mailbox_form_by_id,
    mailbox_form_disabled_features,
    mailbox_form_state_names,
)
from app.services.mcp_tool_contracts import build_report_task_checkpoint_payload


PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
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

    return preflight


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
    start_args = {
        **payload,
        "project": project,
        "task_id": str(payload["task_id"]),
        "owner_agent": str(payload.get("owner_agent") or payload.get("agent_id") or "codex"),
        "session_id": str(payload.get("session_id") or session_id or _generated_mailbox_session_id(payload)),
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
        "work_token": result.get("work_token"),
        "work_session": result.get("work_session"),
        "work_session_resumed": resumed,
        "reclaim": reclaim,
        "auto_heartbeat": result.get("auto_heartbeat"),
        "next_state": "implementation",
        "next_forms": ["record_progress", "finish_task", "release_task_claim"],
        "next_safe_action": "Continue implementation, then submit record_progress or finish_task through mailbox.",
    }
    packet: dict[str, Any] = {"state": state, "project": project, "receipt": _compact(receipt), "next_safe_action": receipt["next_safe_action"]}
    if diagnostic:
        packet["_internal"] = {"visibility": "internal", "actual_metadata": actual_metadata, "postcondition_health": health}
    return packet


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
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "conflict",
                "form_id": form.id,
                "message": public_mailbox_error_message(exc),
                "next_safe_action": "Review closeout evidence and task identity, then submit finish_task again or release_task_claim if only cleanup is needed.",
            },
        }
    try:
        result = json.loads(raw)
    except Exception:
        result = {"status": "error", "message": raw}
    if result.get("status") != "finished":
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": result.get("status") or "conflict",
                "form_id": form.id,
                "message": result.get("message") or result.get("error") or "finish_task_session did not finish.",
                "next_safe_action": result.get("next_safe_action") or "Review the receipt, fix missing evidence, and submit finish_task again.",
            },
        }
    release = dict(result.get("release") or {})
    if isinstance(release.get("lease"), dict):
        release["lease"] = public_lease_payload(release.get("lease"))
    actual_metadata = {"result_kind": "task_finished", "mutation": True}
    health = evaluate_mailbox_postconditions(form, actual_metadata)
    receipt = {
        "status": "finished",
        "form_id": form.id,
        "mode": form.mode,
        "message": "Task session finished and claim release was attempted.",
        "task_id": result.get("task_id"),
        "release": release,
        "next_safe_action": "Request mailbox_state for planning or handoff before starting more work.",
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
    close_status = str(payload.get("close_status") or "obsolete").strip().lower() or "obsolete"
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
    if superseded_by:
        packet["receipt"]["superseded_by"] = superseded_by
    if release_receipt:
        packet["receipt"]["release"] = release_receipt
    packet["next_safe_action"] = "Request state planning or list open tasks before selecting new work."
    packet["receipt"]["next_safe_action"] = packet["next_safe_action"]
    return packet


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
    if risk:
        description_parts.append(f"Risk: {risk}")
    evidence_refs = _string_list_arg(payload.get("evidence_refs"))
    if evidence_refs:
        description_parts.append("Evidence refs: " + ", ".join(evidence_refs))
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
        "tags": ["mailbox", "mcp-fsm", "mcp-improvement", "entity:task", f"task_id:{task_id}", "task_status:planning"],
        "linked_improvement_id": task_id,
    }
    task_result = await dependencies.post(api_base, "/project/tasks", task_payload)
    await dependencies.post(
        api_base,
        f"/project/tasks/{quote(task_id, safe='')}/changes",
        {
            "project": project,
            "change_type": "task_created",
            "content": f"Task bootstrapped from mailbox improvement '{task_payload['title']}'.",
            "why": "Public create_improvement must return a directly usable task_id for weak-model workflows.",
            "agent_id": mailbox_actor(payload),
            "source": "mailbox_submit.create_improvement",
            "tags": ["mailbox", "mcp-fsm", "mcp-improvement"],
        },
    )
    result = {
        "id": str(uid),
        "artifact_key": f"improvement:{project}:{uid}",
        "created": bool(created),
        "title": task_payload["title"],
        "task_id": task_id,
        "linked_artifact_key": f"task:{project}:{task_id}",
        "task_status": task_result.get("status") or "planning",
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
    packet["receipt"]["task_id"] = task_id
    packet["receipt"]["linked_artifact_key"] = result["linked_artifact_key"]
    packet["receipt"]["task_status"] = result["task_status"]
    packet["receipt"]["next_safe_action"] = "Use task_id for start_task, record_progress, finish_task, or close_task."
    packet["next_safe_action"] = packet["receipt"]["next_safe_action"]
    return packet


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
    if task_id:
        lease_guard = dependencies.task_mutation_guard(
            project=project,
            task_id=task_id,
            owner_agent=mailbox_actor(payload),
            owner_session_id=str(payload.get("session_id") or session_id or ""),
            tool_name="mailbox_submit.record_progress",
            work_token=str(payload.get("work_token") or ""),
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
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "conflict",
                    "form_id": form.id,
                    "message": "Task progress requires an active owned claim when task_id is provided.",
                    "recommended_reclaim_call": reclaim_call,
                    "next_safe_action": lease_guard.get("next_safe_action", "Claim the task before recording task progress."),
                },
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
        ("changed_files", _string_list_arg(payload.get("changed_files"))),
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
