from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.task_lease_service import (
    WorkHandleInvalid,
    build_work_handle,
    TaskLeaseConflict,
    TaskLeaseUnavailable,
    WorkTokenMismatch,
    find_continuity_lease_for_mutation,
    get_task_lease_store,
    stop_task_lease_auto_heartbeat,
    verify_work_token_for_mutation,
    work_handle_to_legacy_context,
)


SessionIdentityCallback = Callable[[str | None], Awaitable[dict[str, str]]]


@dataclass(frozen=True)
class TaskLeaseActionDependencies:
    get_session_identity_defaults: SessionIdentityCallback


def task_mutation_requires_owned_claim(
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
    project_clean = str(project or "mnemoforge").strip() or "mnemoforge"
    task_clean = str(task_id or "").strip()
    owner_clean = str(owner_agent or "codex").strip() or "codex"
    session_clean = str(owner_session_id or "").strip()
    work_token_clean = str(work_token or "").strip()
    work_handle_clean = str(work_handle or "").strip()
    if work_handle_clean:
        try:
            handle_context = work_handle_to_legacy_context(
                store=get_task_lease_store(),
                work_handle=work_handle_clean,
                project=project_clean,
                task_id=task_clean,
                owner_agent=owner_clean,
            )
        except WorkHandleInvalid as exc:
            return {
                "status": "conflict",
                "error": exc.reason,
                "tool": tool_name,
                "project": project_clean,
                "task_id": task_clean,
                "claim_allowed": False,
                "next_safe_action": "Use the work_handle returned by start_task_session for this task, or reclaim after the active lease expires.",
            }
        work_token_clean = str(handle_context["work_token"])
        session_clean = str(handle_context["session_id"])
        owner_clean = str(handle_context["owner_agent"])
    bypass_authorized = danger_mode and str(danger_confirmation).strip().lower() == "authorize_session_bypass"

    if not task_clean:
        return {
            "status": "conflict",
            "error": "task_id_required",
            "tool": tool_name,
            "claim_allowed": False,
            "next_safe_action": "Provide task_id for mutating task operations.",
        }

    store = get_task_lease_store()
    active = store.get_active_claim(project=project_clean, task_id=task_clean)
    if active is None:
        continuity_lease = find_continuity_lease_for_mutation(
            store=store,
            project=project_clean,
            task_id=task_clean,
            owner_agent=owner_clean,
            session_id=session_clean,
            work_token=work_token_clean,
        )
        if continuity_lease is not None:
            return None
        if not bypass_authorized:
            return {
                "status": "conflict",
                "error": "active_claim_required",
                "tool": tool_name,
                "project": project_clean,
                "task_id": task_clean,
                "claim_allowed": False,
                "continuity_reclaim_available": bool(work_token_clean and session_clean),
                "next_safe_action": (
                    "Pass the returned work_handle to continue or finish after TTL/session loss; "
                    "otherwise submit start_task to claim available work."
                ),
            }
        return None

    if work_token_clean and verify_work_token_for_mutation(
        store=store,
        lease_id=active.lease_id,
        work_token=work_token_clean,
        task_id=task_clean,
        project=project_clean,
    ):
        return None

    if not session_clean:
        if bypass_authorized:
            session_clean = f"danger-mode-{uuid.uuid4().hex[:8]}"
        else:
            return {
                "status": "conflict",
                "error": "session_id_required_for_mutation",
                "tool": tool_name,
                "claim_allowed": False,
                "next_safe_action": "Claim task first and pass session_id for mutating operations.",
            }

    if active.owner_agent != owner_clean or active.session_id != session_clean:
        if not bypass_authorized:
            return {
                "status": "conflict",
                "error": "lease_owner_mismatch",
                "tool": tool_name,
                "project": project_clean,
                "task_id": task_clean,
                "owner_agent": active.owner_agent,
                "owner_session_id": active.session_id,
                "lease_id": active.lease_id,
                "expires_at": active.expires_at.isoformat(),
                "claim_allowed": False,
                "next_safe_action": "Do not mutate this task; coordinate handoff or wait for lease release/expiry.",
            }

    if not work_token_clean:
        if not bypass_authorized:
            return {
                "status": "conflict",
                "error": "work_token_required",
                "tool": tool_name,
                "lease_id": active.lease_id,
                "claim_allowed": False,
                "next_safe_action": "Pass work_handle from start_task_session for mutating operations.",
            }
    elif not verify_work_token_for_mutation(
        store=store,
        lease_id=active.lease_id,
        work_token=work_token_clean,
        task_id=task_clean,
        project=project_clean,
    ):
        return {
            "status": "conflict",
            "error": "work_token_invalid",
            "tool": tool_name,
            "lease_id": active.lease_id,
            "claim_allowed": False,
            "next_safe_action": "Do not mutate this task; work_token verification failed.",
        }
    return None


async def execute_task_lease_action(
    *,
    name: str,
    args: dict[str, Any],
    dependencies: TaskLeaseActionDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    store = get_task_lease_store()
    owner_agent = str(args.get("owner_agent") or args.get("agent_id") or "codex").strip() or "codex"
    lease_session_id = str(args.get("session_id") or session_id or "").strip()
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    agent_fingerprint = str(args.get("agent_fingerprint") or identity_defaults.get("agent_fingerprint") or "").strip()
    runtime_profile_id = str(args.get("runtime_profile_id") or identity_defaults.get("runtime_profile_id") or "unknown_cli").strip() or "unknown_cli"

    try:
        if name == "claim_task":
            if not lease_session_id:
                raise ValueError("session_id is required for claim_task")
            claim = store.claim(
                project=str(args.get("project") or "mnemoforge"),
                task_id=str(args["task_id"]),
                owner_agent=owner_agent,
                session_id=lease_session_id,
                agent_fingerprint=agent_fingerprint,
                runtime_profile_id=runtime_profile_id,
                work_token=str(args.get("work_token") or ""),
                lease_ttl_seconds=int(args.get("lease_ttl_seconds") or 900),
            )
            data = claim.model_dump(mode="json")
            data["work_handle"] = build_work_handle(lease=claim.lease, work_token=claim.work_token)
            data["next_safe_action"] = "Start or continue work while the task claim is active."
            return data

        if name == "heartbeat_task_claim":
            if not lease_session_id:
                raise ValueError("session_id is required for heartbeat_task_claim")
            lease = store.heartbeat(
                lease_id=str(args["lease_id"]),
                owner_agent=owner_agent,
                session_id=lease_session_id,
                lease_ttl_seconds=int(args["lease_ttl_seconds"]) if args.get("lease_ttl_seconds") else None,
            )
            return {
                "status": "renewed",
                "lease": lease.model_dump(mode="json"),
                "next_safe_action": "Continue work; the task claim expiration was extended.",
            }

        if name == "release_task_claim":
            if not lease_session_id:
                raise ValueError("session_id is required for release_task_claim")
            lease = store.release(
                lease_id=str(args["lease_id"]),
                owner_agent=owner_agent,
                session_id=lease_session_id,
                reason=str(args.get("reason") or "released"),
                status=str(args.get("status") or "released"),
            )
            stop_task_lease_auto_heartbeat(lease.lease_id)
            return {
                "status": lease.status,
                "lease": lease.model_dump(mode="json"),
                "next_safe_action": "The task claim is no longer active.",
            }

        if name == "force_release_task_claim":
            lease = store.force_release(
                lease_id=str(args["lease_id"]),
                acted_by=str(args.get("acted_by") or owner_agent).strip() or owner_agent,
                reason=str(args.get("reason") or "force_released"),
                status=str(args.get("status") or "released"),
            )
            stop_task_lease_auto_heartbeat(lease.lease_id)
            return {
                "status": lease.status,
                "lease": lease.model_dump(mode="json"),
                "force_released": True,
                "next_safe_action": "The task claim was force-released; coordinate before reclaiming.",
            }

        if name == "list_task_claims":
            leases = store.list_leases(
                project=str(args.get("project") or "") or None,
                task_id=str(args.get("task_id") or "") or None,
                owner_agent=str(args.get("owner_agent") or "") or None,
                agent_fingerprint=str(args.get("agent_fingerprint") or identity_defaults.get("agent_fingerprint") or "") or None,
                runtime_profile_id=str(args.get("runtime_profile_id") or identity_defaults.get("runtime_profile_id") or "") or None,
                status=str(args.get("status") or "active"),
                limit=int(args.get("limit") or 50),
            )
            return {
                "status": "listed",
                "count": len(leases),
                "leases": [lease.model_dump(mode="json") for lease in leases],
                "next_safe_action": "Avoid active claims owned by another agent; stale claims are expired before listing.",
            }

        raise ValueError(f"Unsupported task lease action: {name}")

    except TaskLeaseConflict as exc:
        return {
            "status": "conflict",
            "error": exc.to_dict(),
            "claim_allowed": False,
            "owner_agent": exc.active_lease.owner_agent,
            "owner_session_id": exc.active_lease.session_id,
            "expires_at": exc.active_lease.expires_at.isoformat(),
            "next_safe_action": "Do not start this task; choose another task or wait for the claim to expire.",
        }
    except TaskLeaseUnavailable as exc:
        return {
            "status": "conflict",
            "error": exc.to_dict(),
            "claim_allowed": False,
            "lease": exc.lease.model_dump(mode="json"),
            "next_safe_action": "The claim is no longer active; call start_task_session or claim_task again before mutating task state.",
        }
    except WorkTokenMismatch as exc:
        return {
            "status": "conflict",
            "error": "work_token_invalid",
            "claim_allowed": False,
            "lease_id": exc.lease_id,
            "next_safe_action": "Do not reclaim this task; the work_token did not match the active same-fingerprint claim.",
        }
