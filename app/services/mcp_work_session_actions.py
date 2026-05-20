from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.stenographer_service import ProtocolViolation, get_stenographer_store


TaskMutationGuardCallback = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class WorkSessionActionDependencies:
    task_mutation_guard: TaskMutationGuardCallback


def execute_work_session_action(
    *,
    name: str,
    args: dict[str, Any],
    dependencies: WorkSessionActionDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    store = get_stenographer_store()
    agent_id = str(args.get("agent_id") or "codex").strip() or "codex"
    protocol_session_id = str(args.get("session_id") or session_id or agent_id).strip() or agent_id
    try:
        if name == "get_work_session_state":
            return store.get_state(agent_id=agent_id, session_id=protocol_session_id).model_dump(mode="json")

        if name == "start_work_session":
            lease_guard = dependencies.task_mutation_guard(
                project=str(args.get("project") or "mnemoforge"),
                task_id=str(args.get("task_id") or ""),
                owner_agent=agent_id,
                owner_session_id=protocol_session_id,
                tool_name=name,
                work_token=str(args.get("work_token") or ""),
                danger_mode=bool(args.get("danger_mode", False)),
                danger_confirmation=str(args.get("danger_confirmation") or ""),
            )
            if lease_guard:
                return lease_guard
            return store.start_work_session(
                project=str(args.get("project") or "mnemoforge"),
                task_id=str(args["task_id"]),
                agent_id=agent_id,
                session_id=protocol_session_id,
                role=str(args.get("role") or "worker"),
                work_id=str(args.get("work_id") or ""),
                parent_work_id=str(args.get("parent_work_id") or ""),
                parent_task_id=str(args.get("parent_task_id") or ""),
                spawn_reason=str(args.get("spawn_reason") or ""),
                return_condition=str(args.get("return_condition") or ""),
                scope=args.get("scope") or [],
                summary=str(args.get("summary") or ""),
            ).model_dump(mode="json")

        if name == "park_work_session":
            return store.park_work_session(
                work_id=str(args["work_id"]),
                agent_id=agent_id,
                session_id=protocol_session_id,
                reason=str(args["reason"]),
                child_task_id=str(args.get("child_task_id") or ""),
                child_work_id=str(args.get("child_work_id") or ""),
            ).model_dump(mode="json")

        if name == "resume_work_session":
            return store.resume_work_session(
                work_id=str(args["work_id"]),
                agent_id=agent_id,
                session_id=protocol_session_id,
                child_work_id=str(args.get("child_work_id") or ""),
                result=str(args.get("result") or ""),
            ).model_dump(mode="json")

        if name == "end_work_session":
            lease_guard = dependencies.task_mutation_guard(
                project=str(args.get("project") or "mnemoforge"),
                task_id=str(args.get("task_id") or ""),
                owner_agent=agent_id,
                owner_session_id=protocol_session_id,
                tool_name=name,
                work_token=str(args.get("work_token") or ""),
                danger_mode=bool(args.get("danger_mode", False)),
                danger_confirmation=str(args.get("danger_confirmation") or ""),
            )
            if lease_guard:
                return lease_guard
            data = store.end_work_session(
                work_id=str(args["work_id"]),
                task_id=str(args["task_id"]),
                agent_id=agent_id,
                session_id=protocol_session_id,
                status=str(args["status"]),
                result=str(args.get("result") or ""),
            ).model_dump(mode="json")
            _add_completed_work_closeout_hints(data, args)
            return data

        if name == "record_stenographer_span":
            return store.record_span(
                project=str(args.get("project") or "mnemoforge"),
                task_id=str(args.get("task_id") or ""),
                work_id=str(args.get("work_id") or ""),
                agent_id=agent_id,
                session_id=protocol_session_id,
                kind=str(args["kind"]),
                source=str(args.get("source") or ""),
                content=str(args["content"]),
            ).model_dump(mode="json")

        if name == "list_stenographer_spans":
            items = [
                item.model_dump(mode="json")
                for item in store.list_spans(
                    project=str(args.get("project") or "") or None,
                    task_id=str(args.get("task_id") or "") or None,
                    work_id=str(args.get("work_id") or "") or None,
                    agent_id=str(args.get("agent_id") or "") or None,
                    session_id=str(args.get("session_id") or "") or None,
                    limit=int(args.get("limit") or 50),
                )
            ]
            return {"total": len(items), "items": items}

        raise ValueError(f"Unsupported work session action: {name}")

    except ProtocolViolation as exc:
        return exc.to_dict()


def _add_completed_work_closeout_hints(data: dict[str, Any], args: dict[str, Any]) -> None:
    if str(args.get("status") or "").strip() != "completed":
        return
    store = get_stenographer_store()
    spans = store.list_spans(
        project=str(data.get("project") or "") or None,
        task_id=str(args["task_id"]),
        work_id=str(args["work_id"]),
        limit=50,
    )
    if not spans:
        return
    data["stenographer_span_count"] = len(spans)

    from app.services.checkpoint_draft_service import get_checkpoint_draft_store

    latest_draft = get_checkpoint_draft_store().latest_for_work(
        project=str(data.get("project") or ""),
        task_id=str(args["task_id"]),
        work_id=str(args["work_id"]),
    )
    if latest_draft and latest_draft.status == "approved":
        data["approved_checkpoint_draft_id"] = latest_draft.draft_id
        data["saved_change_id"] = latest_draft.saved_change_id
        data["recommended_next_tool"] = "get_task_execution_context"
        data["closeout_notice"] = (
            "Stenographer spans already have an approved checkpoint draft. "
            "Use get_task_execution_context for the next operational step."
        )
    else:
        data["recommended_next_tool"] = "clerk_draft_report"
        data["closeout_notice"] = (
            "Stenographer spans exist for this completed work session. "
            "Use clerk_draft_report to structure them into a review-only checkpoint/report draft before governed memory mutation."
        )
