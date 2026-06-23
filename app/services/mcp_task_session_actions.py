from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.mcp_tool_contracts import build_report_task_checkpoint_payload
from app.services.stenographer_service import ProtocolViolation, get_stenographer_store
from app.services.task_lease_service import (
    TaskLeaseConflict,
    WorkTokenMismatch,
    find_continuity_lease_for_mutation,
    get_task_lease_store,
    start_task_lease_auto_heartbeat,
    stop_task_lease_auto_heartbeat,
    verify_work_token_for_mutation,
)


GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
SessionIdentityCallback = Callable[[str | None], Awaitable[dict[str, str]]]


@dataclass(frozen=True)
class TaskSessionActionDependencies:
    post: PostCallback
    get_session_identity_defaults: SessionIdentityCallback
    get: GetCallback | None = None


async def start_task_session_action(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: TaskSessionActionDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args["task_id"]).strip()
    owner_agent = str(args.get("owner_agent") or args.get("agent_id") or "codex").strip() or "codex"
    lease_session_id = str(args.get("session_id") or session_id or "").strip()
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    agent_fingerprint = str(args.get("agent_fingerprint") or identity_defaults.get("agent_fingerprint") or "").strip()
    runtime_profile_id = str(args.get("runtime_profile_id") or identity_defaults.get("runtime_profile_id") or "unknown_cli").strip() or "unknown_cli"

    danger_mode = bool(args.get("danger_mode", False))
    danger_confirmation = str(args.get("danger_confirmation", "")).strip().lower()
    if not lease_session_id:
        if danger_mode and danger_confirmation == "authorize_session_bypass":
            lease_session_id = f"danger-mode-{uuid.uuid4().hex[:8]}"
        else:
            raise ValueError("session_id is required for start_task_session. Set danger_mode=true with danger_confirmation='authorize_session_bypass' for recovery operations.")

    lease_store = get_task_lease_store()
    session_store = get_stenographer_store()
    claim = None
    auto_heartbeat_enabled = bool(args.get("auto_heartbeat", True))
    auto_heartbeat = None
    work_session_resumed = False
    try:
        claim = lease_store.claim(
            project=project,
            task_id=task_id,
            owner_agent=owner_agent,
            session_id=lease_session_id,
            agent_fingerprint=agent_fingerprint,
            runtime_profile_id=runtime_profile_id,
            work_token=str(args.get("work_token") or ""),
            lease_ttl_seconds=int(args.get("lease_ttl_seconds") or 900),
        )
        work = session_store.get_active_work_by_task(
            project=project,
            task_id=task_id,
            agent_id=owner_agent,
            session_id=lease_session_id,
        )
        work_session_resumed = work is not None
        if work is None:
            work = session_store.start_work_session(
                project=project,
                task_id=task_id,
                agent_id=owner_agent,
                session_id=lease_session_id,
                role=str(args.get("role") or "worker"),
                work_id=str(args.get("work_id") or ""),
                parent_work_id=str(args.get("parent_work_id") or ""),
                parent_task_id=str(args.get("parent_task_id") or ""),
                spawn_reason=str(args.get("spawn_reason") or ""),
                return_condition=str(args.get("return_condition") or ""),
                scope=args.get("scope") or [],
                summary=str(args.get("summary") or ""),
            )
        if auto_heartbeat_enabled:
            auto_heartbeat = start_task_lease_auto_heartbeat(
                store=lease_store,
                lease=claim.lease,
                heartbeat_seconds=float(args["heartbeat_seconds"]) if args.get("heartbeat_seconds") else None,
            )
    except TaskLeaseConflict as exc:
        return {
            "status": "conflict",
            "error": exc.to_dict(),
            "claim_allowed": False,
            "owner_agent": exc.active_lease.owner_agent,
            "owner_session_id": exc.active_lease.session_id,
            "expires_at": exc.active_lease.expires_at.isoformat(),
            "next_safe_action": "Task is already claimed by another session. Do not start work session.",
        }
    except WorkTokenMismatch as exc:
        return {
            "status": "conflict",
            "error": "work_token_invalid",
            "claim_allowed": False,
            "lease_id": exc.lease_id,
            "next_safe_action": "Task has a matching fingerprint but the work_token did not verify; do not start a replacement session.",
        }
    except ProtocolViolation as exc:
        if claim is not None:
            stop_task_lease_auto_heartbeat(claim.lease.lease_id)
            try:
                lease_store.release(
                    lease_id=claim.lease.lease_id,
                    owner_agent=owner_agent,
                    session_id=lease_session_id,
                    reason="start_task_session_rollback",
                )
            except Exception:
                pass
        raise ValueError(str(exc)) from exc

    try:
        checkpoint_payload = build_report_task_checkpoint_payload(
            {
                "project": project,
                "task_id": task_id,
                "stage": "in_progress",
                "summary": str(args.get("summary") or "Task claimed; work session started."),
                "status": "active",
                "reason": str(args.get("reason") or "start_task_session"),
                "acted_by": str(args.get("acted_by") or owner_agent),
                "source": str(args.get("source") or "start_task_session"),
                "checkpoint_mode": str(args.get("checkpoint_mode") or "lightweight"),
            }
        )
        checkpoint = await dependencies.post(
            api_base,
            f"/project/tasks/{quote(task_id, safe='')}/changes",
            checkpoint_payload,
        )
    except Exception:
        if claim is not None:
            stop_task_lease_auto_heartbeat(claim.lease.lease_id)
        try:
            session_store.end_work_session(
                work_id=work.work_id,
                task_id=task_id,
                agent_id=owner_agent,
                session_id=lease_session_id,
                status="interrupted",
                result="start_task_session rollback after checkpoint write failure",
            )
        except Exception:
            pass
        try:
            lease_store.release(
                lease_id=claim.lease.lease_id,
                owner_agent=owner_agent,
                session_id=lease_session_id,
                reason="start_task_session_checkpoint_failed",
            )
        except Exception:
            pass
        raise

    return {
        "status": "started",
        "project": project,
        "task_id": task_id,
        "owner_agent": owner_agent,
        "owner_session_id": lease_session_id,
        "lease": claim.lease.model_dump(mode="json"),
        "lease_status": claim.status,
        "same_fingerprint_reclaim": claim.same_fingerprint_reclaim,
        "previous_claim_expired": claim.previous_claim_expired,
        "previous_lease": claim.previous_lease.model_dump(mode="json") if claim.previous_lease else None,
        "work_token": claim.work_token,
        "auto_heartbeat": {
            "enabled": auto_heartbeat_enabled,
            "heartbeat_seconds": auto_heartbeat.heartbeat_seconds if auto_heartbeat is not None else None,
        },
        "work_session": work.model_dump(mode="json"),
        "work_session_resumed": work_session_resumed,
        "checkpoint": checkpoint,
        "next_safe_action": (
            "Continue implementation; lease auto-heartbeat is active for this process and finish_task_session will stop it."
            if auto_heartbeat_enabled
            else "Continue implementation and send heartbeat_task_claim while session is active."
        ),
    }


async def finish_task_session_action(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: TaskSessionActionDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args["task_id"]).strip()
    owner_agent = str(args.get("owner_agent") or args.get("agent_id") or "codex").strip() or "codex"
    lease_session_id = str(args.get("session_id") or session_id or "").strip()
    work_token = str(args.get("work_token") or "").strip()

    danger_mode = bool(args.get("danger_mode", False))
    danger_confirmation = str(args.get("danger_confirmation", "")).strip().lower()
    if not lease_session_id and not work_token:
        if danger_mode and danger_confirmation == "authorize_session_bypass":
            lease_session_id = f"danger-mode-{uuid.uuid4().hex[:8]}"
        else:
            raise ValueError("session_id is required for finish_task_session. Set danger_mode=true with danger_confirmation='authorize_session_bypass' for recovery operations.")

    lease_store = get_task_lease_store()
    active = lease_store.get_active_claim(project=project, task_id=task_id)
    continuity_lease = None
    continuity_reclaim = False
    if active is None:
        continuity_lease = find_continuity_lease_for_mutation(
            store=lease_store,
            project=project,
            task_id=task_id,
            owner_agent=owner_agent,
            session_id=lease_session_id,
            work_token=work_token,
        )
        continuity_reclaim = continuity_lease is not None
        if not continuity_reclaim and not (danger_mode and danger_confirmation == "authorize_session_bypass"):
            return {
                "status": "conflict",
                "error": "active_claim_required",
                "project": project,
                "task_id": task_id,
                "claim_allowed": False,
                "continuity_reclaim_available": bool(work_token and lease_session_id),
                "recommended_reclaim_call": {
                    "tool": "submit",
                    "form_id": "finish_task",
                    "payload_fields": ["project", "task_id", "owner_agent", "session_id", "work_id", "work_token"],
                },
                "next_safe_action": "Pass original owner_agent, session_id, work_id, and work_token to finish after TTL/session loss, or call start_task to claim available work.",
            }

    work_token_valid = False
    if active is not None and work_token:
        work_token_valid = verify_work_token_for_mutation(
            store=lease_store,
            lease_id=active.lease_id,
            work_token=work_token,
            task_id=task_id,
            project=project,
        )
        if work_token_valid:
            lease_session_id = active.session_id

    if active is not None and not work_token_valid and (active.owner_agent != owner_agent or active.session_id != lease_session_id):
        if not (danger_mode and danger_confirmation == "authorize_session_bypass"):
            return {
                "status": "conflict",
                "error": "lease_owner_mismatch",
                "project": project,
                "task_id": task_id,
                "owner_agent": active.owner_agent,
                "owner_session_id": active.session_id,
                "lease_id": active.lease_id,
                "expires_at": active.expires_at.isoformat(),
                "claim_allowed": False,
                "next_safe_action": "Do not finish or mutate this task; coordinate handoff or wait for lease release/expiry.",
            }

    session_store = get_stenographer_store()
    explicit_work_id = str(args.get("work_id") or "").strip()
    if explicit_work_id:
        work_id = explicit_work_id
    else:
        active_work = session_store.get_active_work_by_task(
            project=project,
            task_id=task_id,
            agent_id=owner_agent,
            session_id=lease_session_id,
        )
        if active_work is None and work_token_valid:
            active_work = session_store.get_active_work_by_task_any_session(
                project=project,
                task_id=task_id,
                agent_id=owner_agent,
            )
        if active_work is None and continuity_reclaim:
            active_work = session_store.get_active_work_by_task_any_session(
                project=project,
                task_id=task_id,
                agent_id=owner_agent,
            )
        if active_work is None:
            return {
                "status": "conflict",
                "error": "work_session_not_found",
                "project": project,
                "task_id": task_id,
                "message": "No active work session found for this task. Provide work_id explicitly or start a work session first.",
                "next_safe_action": "Provide work_id parameter or call start_work_session first.",
            }
        work_id = active_work.work_id

    checkpoint_payload = build_report_task_checkpoint_payload(
        {
            "project": project,
            "task_id": task_id,
            "stage": "completed",
            "summary": str(args.get("summary") or "Task session finished."),
            "status": "done",
            "changed_files": _string_list_arg(args.get("changed_files")),
            "verification": _string_list_arg(args.get("verification")),
            "next_step": str(args.get("next_step") or "").strip(),
            "next_step_scope": str(args.get("next_step_scope") or "none").strip() or "none",
            "reason": str(args.get("reason") or "finish_task_session"),
            "acted_by": str(args.get("acted_by") or owner_agent),
            "source": str(args.get("source") or "finish_task_session"),
            "checkpoint_mode": str(args.get("checkpoint_mode") or "standard"),
        }
    )
    checkpoint = await dependencies.post(
        api_base,
        f"/project/tasks/{quote(task_id, safe='')}/changes",
        checkpoint_payload,
    )

    if str(args.get("status") or "completed") == "completed":
        for item in _string_list_arg(args.get("verification")):
            session_store.record_span(
                project=project,
                task_id=task_id,
                work_id=work_id,
                agent_id=owner_agent,
                session_id=lease_session_id,
                kind="verification",
                source="finish_task_session",
                content=item,
            )
        for item in _closeout_span_values(args, "changed_files"):
            session_store.record_span(
                project=project,
                task_id=task_id,
                work_id=work_id,
                agent_id=owner_agent,
                session_id=lease_session_id,
                kind="changed_files",
                source="finish_task_session",
                content=item,
            )
        next_step_text = str(args.get("next_step") or "").strip()
        if next_step_text:
            session_store.record_span(
                project=project,
                task_id=task_id,
                work_id=work_id,
                agent_id=owner_agent,
                session_id=lease_session_id,
                kind="next_step",
                source="finish_task_session",
                content=next_step_text,
            )
    try:
        if work_token_valid or continuity_reclaim:
            work = session_store.end_work_session_by_work_id(
                work_id=work_id,
                status=str(args.get("status") or "completed"),
                result=str(args.get("result") or ""),
            )
        else:
            work = session_store.end_work_session(
                work_id=work_id,
                task_id=task_id,
                agent_id=owner_agent,
                session_id=lease_session_id,
                status=str(args.get("status") or "completed"),
                result=str(args.get("result") or ""),
            )
    except ProtocolViolation as exc:
        raise ValueError(str(exc)) from exc

    if active is not None:
        release_session_id = active.session_id if work_token_valid else lease_session_id
        released = lease_store.release(
            lease_id=active.lease_id,
            owner_agent=owner_agent,
            session_id=release_session_id,
            reason=str(args.get("release_reason") or "finished"),
            status="released",
        )
        stop_task_lease_auto_heartbeat(active.lease_id)
        release_payload = {
            "status": released.status,
            "lease": released.model_dump(mode="json"),
        }
    else:
        if continuity_reclaim and continuity_lease is not None:
            release_payload = {
                "status": "continuity_reclaim",
                "note": "No active lease existed; same-owner continuity evidence authorized finish without a manual start_task workaround.",
                "lease": continuity_lease.model_dump(mode="json"),
            }
        else:
            release_payload = {
                "status": "bypassed",
                "note": "No active lease to release; danger_mode bypass was used.",
            }

    resolved = False
    get = dependencies.get
    if get is not None:
        for artifact_type in ("improvement", "task"):
            artifact_key = f"{artifact_type}:{quote(project, safe='')}:{quote(task_id, safe='')}"
            try:
                artifact = await get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
                if artifact and artifact.get("status") in ("open", "active"):
                    await dependencies.post(
                        api_base,
                        f"/artifacts/{quote(artifact_key, safe='')}/resolve",
                        {
                            "acted_by": owner_agent,
                            "action_source": "finish_task_session",
                            "reason": "Task session finished.",
                        },
                    )
                    resolved = True
                    break
            except Exception:
                continue

    if not resolved:
        try:
            await dependencies.post(
                api_base,
                f"/project/tasks/{quote(task_id, safe='')}/reopen",
                {
                    "status": "done",
                    "reason": "finish_task_session",
                    "acted_by": owner_agent,
                    "source": "finish_task_session",
                },
            )
        except Exception:
            logging.getLogger(__name__).info(
                "finish_task_session: no artifact or project task to close for task %s", task_id
            )

    return {
        "status": "finished",
        "project": project,
        "task_id": task_id,
        "owner_agent": owner_agent,
        "owner_session_id": lease_session_id,
        "checkpoint": checkpoint,
        "work_session": work.model_dump(mode="json"),
        "release": release_payload,
        "continuity_reclaim": continuity_reclaim,
        "continuity_lease": continuity_lease.model_dump(mode="json") if continuity_lease else None,
        "next_safe_action": (
            "Task finished by same-owner continuity evidence after TTL/session loss."
            if continuity_reclaim
            else "If release.status is conflict, coordinate with current owner or use force_release_task_claim."
        ),
    }


def _string_list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _closeout_span_values(args: dict[str, Any], key: str) -> list[str]:
    values = _string_list_arg(args.get(key))
    if values:
        return values
    if key == "changed_files" and key in args:
        return ["none"]
    return []
