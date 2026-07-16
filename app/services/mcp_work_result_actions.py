"""record_work_result MCP action implementation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.mcp_lifecycle_receipts import public_auto_work_session_payload
from app.services.mcp_tool_contracts import build_report_task_checkpoint_payload

PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
AnnotateCallback = Callable[[str, Any], Any]
ResolveTargetCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
MutationGuardCallback = Callable[..., dict[str, Any] | None]
CanAutoStartCallback = Callable[..., bool]
AutoStartCallback = Callable[..., Awaitable[dict[str, Any]]]
StenographerSpansCallback = Callable[..., list[dict[str, Any]]]
StringListCallback = Callable[[Any], list[str]]
ErrorFormatter = Callable[[Exception], str]


@dataclass(frozen=True)
class WorkResultActionDependencies:
    post: PostCallback
    annotate_payload: AnnotateCallback
    resolve_target: ResolveTargetCallback
    mutation_requires_owned_claim: MutationGuardCallback
    can_auto_start_checkpoint_session: CanAutoStartCallback
    auto_start_checkpoint_work_session: AutoStartCallback
    available_stenographer_spans: StenographerSpansCallback
    string_list_arg: StringListCallback
    format_error: ErrorFormatter


async def execute_work_result_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    session_id: str | None,
    dependencies: WorkResultActionDependencies,
) -> str:
    if name != "record_work_result":
        raise ValueError(f"Unsupported work result action: {name}")

    target = await dependencies.resolve_target(api_base, args)
    project = target["project"]
    auto_work_session = args.get("_auto_work_session") if isinstance(args.get("_auto_work_session"), dict) else None
    if target.get("task_id"):
        lease_guard = dependencies.mutation_requires_owned_claim(
            project=project,
            task_id=str(target["task_id"]),
            owner_agent=str(args.get("owner_agent") or args.get("agent_id") or args.get("acted_by") or "codex"),
            owner_session_id=str(args.get("session_id") or session_id or ""),
            tool_name=name,
            work_token=str(args.get("work_token") or ""),
            work_handle=str(args.get("work_handle") or ""),
            danger_mode=bool(args.get("danger_mode", False)),
            danger_confirmation=str(args.get("danger_confirmation") or ""),
        )
        if lease_guard and dependencies.can_auto_start_checkpoint_session(lease_guard=lease_guard, args=args):
            auto_start = await dependencies.auto_start_checkpoint_work_session(
                api_base=api_base,
                project=project,
                task_id=str(target["task_id"]),
                args=args,
                session_id=session_id,
                source="record_work_result",
            )
            if str(auto_start.get("status") or "") == "started":
                auto_work_session = public_auto_work_session_payload(auto_start)
                args = {
                    **args,
                    "work_handle": auto_start.get("work_handle"),
                    "session_id": auto_start.get("owner_session_id"),
                    "owner_agent": auto_start.get("owner_agent"),
                    "_auto_work_session": auto_work_session,
                }
                lease_guard = None
            elif auto_start:
                lease_guard = auto_start
        if lease_guard:
            lease_guard = dependencies.annotate_payload(name, lease_guard)
            return json.dumps(lease_guard, indent=2, ensure_ascii=False)

    summary = str(args["summary"]).strip()
    title = str(args.get("title") or "Agent work result").strip() or "Agent work result"
    changed_files = dependencies.string_list_arg(args.get("changed_files"))
    verification = dependencies.string_list_arg(args.get("verification"))
    decisions = dependencies.string_list_arg(args.get("decisions"))
    blockers = dependencies.string_list_arg(args.get("blockers"))
    remaining_risk = dependencies.string_list_arg(args.get("remaining_risk"))
    next_step = str(args.get("next_step") or "").strip()
    source = str(args.get("source") or "record_work_result").strip() or "record_work_result"
    agent_id = str(args.get("agent_id") or args.get("acted_by") or "codex").strip() or "codex"

    memory_lines = [title, "", summary]
    if changed_files:
        memory_lines.append("Changed files: " + ", ".join(changed_files))
    if verification:
        memory_lines.append("Verification: " + "; ".join(verification))
    if decisions:
        memory_lines.append("Decisions: " + "; ".join(decisions))
    if blockers:
        memory_lines.append("Blockers: " + "; ".join(blockers))
    if remaining_risk:
        memory_lines.append("Remaining risk: " + "; ".join(remaining_risk))
    if next_step:
        memory_lines.append("Next step: " + next_step)
    memory_payload = {
        "content": "\n".join(memory_lines),
        "agent_id": agent_id,
        "memory_type": "task",
        "category": "work_result",
        "project": project,
        "importance_score": float(args.get("importance_score") or 0.65),
        "source": source,
        "tags": [
            "work_result",
            f"project:{project}",
            f"target:{target['target_source']}",
        ],
    }
    if target.get("task_id"):
        memory_payload["tags"].append(f"task_id:{target['task_id']}")
    memory_result = await dependencies.post(api_base, "/memories", memory_payload)

    created_issue = None
    clerk_draft = None
    checkpoint_result = None
    resolve_result = None
    route = ["memory"]
    warnings: list[str] = []

    if not target.get("task_id") and bool(args.get("create_issue_if_unmatched", False)):
        source_memory_tag = f"source_memory:{memory_result.get('id')}" if memory_result.get("id") else ""
        created_issue = await dependencies.post(
            api_base,
            "/improvements",
            {
                "project": project,
                "title": title,
                "description": summary,
                "agent_id": agent_id,
                "importance_score": float(args.get("importance_score") or 0.65),
                "stage": "proposal",
                "tags": [
                    tag
                    for tag in ("work-result", "mcp-facade", f"project:{project}", source_memory_tag)
                    if tag
                ],
            },
        )
        route.append("improvement")

    if target.get("task_id"):
        available_spans = dependencies.available_stenographer_spans(args, default_project=project, default_task_id=target["task_id"])
        if available_spans:
            try:
                from app.dependencies import get_llm_gateway
                from app.services.checkpoint_draft_service import draft_checkpoint_from_spans

                draft_args = {
                    "project": project,
                    "task_id": target["task_id"],
                    "work_id": str(args.get("work_id") or "").strip(),
                    "agent_id": agent_id,
                    "session_id": str(args.get("session_id") or "").strip(),
                    "stage": str(args.get("stage") or "completed").strip() or "completed",
                    "status": str(args.get("status") or "done").strip() or "done",
                    "reason": str(args.get("reason") or "record_work_result_clerk_draft").strip(),
                    "use_llm": bool(args.get("clerk_use_llm", args.get("use_llm", False))),
                    "preserve_evidence": bool(args.get("preserve_evidence", True)),
                    "limit": int(args.get("clerk_span_limit") or args.get("limit") or 50),
                }
                clerk_record = await draft_checkpoint_from_spans(draft_args, get_llm_gateway())
                clerk_draft = clerk_record.model_dump(mode="json")
                clerk_draft["mutates_memory"] = False
                clerk_draft["recommended_next_tool"] = (
                    "approve_checkpoint_draft"
                    if clerk_draft.get("validation_report", {}).get("can_approve")
                    else "revise_checkpoint_draft"
                )
                route.append("clerk_draft")
                warnings.append(
                    "Stenographer spans were available, so record_work_result created a review-only clerk draft instead of writing a direct task checkpoint."
                )
                if bool(args.get("should_resolve_artifact", False)):
                    warnings.append("Artifact was not resolved because clerk draft approval is required before lifecycle closure.")
            except Exception as exc:
                warnings.append(f"Clerk draft failed; falling back to direct checkpoint: {dependencies.format_error(exc)}")

        if clerk_draft is not None:
            data = {
                "status": "drafted",
                "project": project,
                "route": route,
                "target": target,
                "memory": {"id": memory_result.get("id")},
                "clerk_draft": {
                    "draft_id": clerk_draft.get("draft_id"),
                    "version": clerk_draft.get("version"),
                    "status": clerk_draft.get("status"),
                    "recommended_next_tool": clerk_draft.get("recommended_next_tool"),
                    "validation_report": clerk_draft.get("validation_report"),
                    "source_span_ids": clerk_draft.get("source_span_ids"),
                },
                "checkpoint": None,
                "auto_work_session": auto_work_session,
                "created_issue": created_issue,
                "resolved_artifact": None,
                "warnings": warnings,
                "next_action": clerk_draft.get("recommended_next_tool") or "Review clerk draft before persisting checkpoint.",
            }
            data = dependencies.annotate_payload(name, data)
            return json.dumps(data, indent=2, ensure_ascii=False)

        stage = str(args.get("stage") or "completed").strip().lower()
        checkpoint_args = {
            "project": project,
            "task_id": target["task_id"],
            "stage": stage,
            "summary": summary,
            "checkpoint_mode": str(args.get("checkpoint_mode") or "standard").strip() or "standard",
            "changed_files": changed_files,
            "verification": verification,
            "decisions": decisions,
            "blockers": blockers,
            "remaining_risk": remaining_risk,
            "next_step": next_step,
            "next_step_scope": str(args.get("next_step_scope") or "none").strip() or "none",
            "status": args.get("status") or ("done" if stage == "completed" else "active"),
            "reason": str(args.get("reason") or "record_work_result closeout").strip(),
            "acted_by": str(args.get("acted_by") or agent_id).strip() or agent_id,
            "source": source,
        }
        checkpoint_payload = build_report_task_checkpoint_payload(checkpoint_args)
        checkpoint_result = await dependencies.post(
            api_base,
            f"/project/tasks/{quote(target['task_id'], safe='')}/changes",
            checkpoint_payload,
        )
        route.append("task_checkpoint")
        if checkpoint_result.get("id"):
            checkpoint_result["stage_evidence"] = f"checkpoint:{checkpoint_result['id']}"

        if bool(args.get("should_resolve_artifact", False)):
            artifact_to_resolve = target.get("artifact_key") or f"task:{project}:{target['task_id']}"
            resolve_result = await dependencies.post(
                api_base,
                f"/artifacts/{quote(artifact_to_resolve, safe='')}/resolve",
                {
                    "acted_by": str(args.get("acted_by") or agent_id).strip() or agent_id,
                    "action_source": source,
                    "reason": summary,
                },
            )
            route.append("resolve_artifact")
    elif not created_issue:
        warnings.append(
            "No task_id/artifact_key was provided and no open task could be matched; recorded memory-only result."
        )

    data = {
        "status": "recorded",
        "project": project,
        "route": route,
        "target": target,
        "memory": {"id": memory_result.get("id")},
        "checkpoint": checkpoint_result,
        "auto_work_session": auto_work_session,
        "created_issue": created_issue,
        "resolved_artifact": resolve_result,
        "warnings": warnings,
        "next_action": (
            "Review unresolved follow-up before resolving artifact."
            if remaining_risk or blockers or next_step
            else "No immediate follow-up recorded."
        ),
    }
    data = dependencies.annotate_payload(name, data)
    return json.dumps(data, indent=2, ensure_ascii=False)
