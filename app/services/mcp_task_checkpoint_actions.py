from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.mcp_tool_contracts import (
    build_report_task_checkpoint_payload,
    format_task_checkpoint_response,
)


GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
TaskMutationGuardCallback = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class TaskCheckpointActionDependencies:
    post: PostCallback
    get: GetCallback
    task_mutation_guard: TaskMutationGuardCallback


_CHECKPOINT_HANDOFF_STAGES = {"blocked", "interrupted", "handoff", "completed"}
_CHECKPOINT_SCOPE_CONFIRMATION = "current checkpoint belongs to this task"
_CHECKPOINT_SCOPE_STOPWORDS = {
    "a", "about", "across", "add", "added", "agent", "agents", "all", "also", "an", "and", "any",
    "api", "are", "artifact", "artifacts", "as", "at", "backed", "be", "because", "by", "can",
    "checkpoint", "checkpoints", "code", "condition", "conditions", "context", "current", "data",
    "db", "decision", "decisions", "doc", "docs", "documentation", "done", "for", "from",
    "generated", "has", "have", "if", "in", "into", "is", "it", "its", "memory", "mcp", "must",
    "new", "not", "of", "on", "or", "path", "project", "projects", "record", "release", "safe",
    "server", "should", "source", "state", "status", "step", "mnemoforge", "task", "tasks",
    "test", "tests", "that", "the", "this", "to", "tool", "use", "user", "using", "with", "work",
}


async def execute_task_checkpoint_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: TaskCheckpointActionDependencies,
    session_id: str | None = None,
) -> str:
    lease_guard = dependencies.task_mutation_guard(
        project=str(args.get("project") or "mnemoforge"),
        task_id=str(args.get("task_id") or ""),
        owner_agent=str(args.get("owner_agent") or args.get("agent_id") or args.get("acted_by") or "codex"),
        owner_session_id=str(args.get("session_id") or session_id or ""),
        tool_name=name,
        work_token=str(args.get("work_token") or ""),
        danger_mode=bool(args.get("danger_mode", False)),
        danger_confirmation=str(args.get("danger_confirmation") or ""),
    )
    if lease_guard:
        return json.dumps(lease_guard, indent=2, ensure_ascii=False)

    payload = build_report_task_checkpoint_payload(args)
    task_id = str(args["task_id"]).strip()
    stage = str(args["stage"]).strip().lower()
    status = _status_from_args(args, payload)
    await _patch_checkpoint_context(session_id, args=args, task_id=task_id, stage=stage, status=status, recorded=False)

    scope_guard_error = await checkpoint_scope_guard(api_base, args, get=dependencies.get)
    if scope_guard_error:
        return json.dumps(scope_guard_error, indent=2, ensure_ascii=False)

    data = await dependencies.post(api_base, f"/project/tasks/{quote(task_id, safe='')}/changes", payload)
    if data.get("id"):
        data["stage_evidence"] = f"checkpoint:{data['id']}"

    handoff_data = None
    handoff_error = None
    if name == "record_task_checkpoint" and stage in _CHECKPOINT_HANDOFF_STAGES:
        try:
            handoff_data = await dependencies.post(
                api_base,
                "/models/handoff",
                checkpoint_handoff_payload(args, stage=stage, status=status),
            )
        except Exception as exc:
            handoff_error = str(exc)

    await _patch_checkpoint_context(session_id, args=args, task_id=task_id, stage=stage, status=status, recorded=True, stage_evidence=str(data.get("stage_evidence") or ""))

    data["task_id"] = task_id
    data["stage"] = stage
    data["status"] = status
    if handoff_data:
        data["handoff_packet_created"] = True
        data["handoff_memory_id"] = handoff_data.get("memory_id")
        data["handoff_label"] = handoff_data.get("handoff_label")
    elif name == "record_task_checkpoint":
        data["handoff_packet_created"] = False
        if handoff_error:
            data["handoff_error"] = handoff_error
    return format_task_checkpoint_response(data)


def checkpoint_handoff_payload(args: dict[str, Any], *, stage: str, status: str) -> dict[str, Any]:
    project = str(args["project"]).strip()
    task_id = str(args["task_id"]).strip()
    summary = str(args["summary"]).strip()
    next_step = str(args.get("next_step") or "").strip()
    blockers = _string_list_arg(args.get("blockers"))
    decisions = _string_list_arg(args.get("decisions"))
    verification = _string_list_arg(args.get("verification"))
    remaining_risk = _string_list_arg(args.get("remaining_risk"))
    changed_files = _string_list_arg(args.get("changed_files"))
    acted_by = str(args.get("acted_by") or "mcp-agent").strip() or "mcp-agent"
    to_agent = str(args.get("to_agent") or acted_by).strip() or acted_by
    key_facts = [*decisions, *verification, *remaining_risk][:10]
    partial_parts = []
    if blockers:
        partial_parts.append("Blockers: " + "; ".join(blockers))
    if next_step:
        partial_parts.append("Next step: " + next_step)
    if remaining_risk:
        partial_parts.append("Remaining risk: " + "; ".join(remaining_risk))
    return {
        "from_agent": acted_by,
        "to_agent": to_agent,
        "project_id": project,
        "phase": stage,
        "priority": "high" if blockers or stage in {"blocked", "interrupted"} else "medium",
        "owner_agent": to_agent,
        "write_scope": changed_files or args.get("write_scope", []),
        "why_now": str(args.get("reason") or f"Resume-relevant checkpoint at stage={stage}.").strip(),
        "definition_of_done": "Resume from this checkpoint, preserve recorded task state, and update task progress before stopping.",
        "expected_output_shape": "Short result summary, verification summary, remaining risks, and next checkpoint.",
        "phase_objective": next_step or summary,
        "execution_mode": "balanced",
        "task_description": f"Checkpoint for task {task_id}: {summary}",
        "partial_result": "\n".join(partial_parts) or None,
        "key_facts": key_facts,
        "task_id": task_id,
        "handoff_label": checkpoint_handoff_label(args, stage),
        "reason": "checkpoint",
        "agent_id": "handoff",
    }


def checkpoint_handoff_label(args: dict[str, Any], stage: str) -> str:
    label = str(args.get("handoff_label") or "").strip().lower()
    if not label:
        task_id = re.sub(r"[^a-z0-9_-]+", "-", str(args.get("task_id") or "").strip().lower()).strip("-_")
        label = f"checkpoint-{(task_id or 'task')[:24]}-{stage}"
    label = re.sub(r"[^a-z0-9_-]+", "-", label).strip("-_")
    if not label or not re.match(r"^[a-z0-9]", label):
        label = f"checkpoint-{label or stage}"
    return label[:64]


async def checkpoint_scope_guard(
    api_base: str,
    args: dict[str, Any],
    *,
    get: GetCallback,
) -> dict[str, Any] | None:
    confirmation = str(args.get("scope_confirmation") or "").strip().lower()
    if confirmation == _CHECKPOINT_SCOPE_CONFIRMATION:
        return None
    project = str(args.get("project") or "").strip()
    task_id = str(args.get("task_id") or "").strip()
    if not project or not task_id:
        return None
    try:
        task = await get(api_base, f"/project/tasks/{quote(task_id, safe='')}?project={quote(project, safe='')}")
    except Exception:
        return None
    decision = checkpoint_scope_guard_decision(args, task)
    if not decision["blocked"]:
        return None
    return {
        "error": "checkpoint_scope_mismatch",
        "task_checkpoint_recorded": False,
        "project": project,
        "task_id": task_id,
        "task_title": task.get("title") or "",
        "summary": str(args.get("summary") or "").strip(),
        "scope_guard": decision,
        "message": (
            "Checkpoint text does not appear to belong to the selected task. "
            "Use list_open_tasks/list_artifacts to choose the right task, create a new task for the shifted topic, "
            f"or pass scope_confirmation='{_CHECKPOINT_SCOPE_CONFIRMATION}' only after human review."
        ),
        "recommended_next_tools": ["list_open_tasks", "list_artifacts", "reopen_task"],
    }


def checkpoint_scope_guard_decision(args: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    task_text = "\n".join(
        str(part)
        for part in (
            task.get("title"),
            task.get("description"),
            " ".join(str(tag) for tag in (task.get("tags") or [])),
        )
        if str(part or "").strip()
    )
    checkpoint_text = _checkpoint_scope_text(args)
    task_tokens = _checkpoint_scope_tokens(task_text)
    title_tokens = _checkpoint_scope_tokens(str(task.get("title") or ""))
    checkpoint_tokens = _checkpoint_scope_tokens(checkpoint_text)
    overlap = sorted(task_tokens & checkpoint_tokens)
    title_overlap = sorted(title_tokens & checkpoint_tokens)
    blocked = bool(task_tokens and checkpoint_tokens and len(overlap) < 2 and not title_overlap)
    return {
        "blocked": blocked,
        "overlap": overlap[:12],
        "title_overlap": title_overlap[:8],
        "task_token_count": len(task_tokens),
        "checkpoint_token_count": len(checkpoint_tokens),
    }


async def _patch_checkpoint_context(
    session_id: str | None,
    *,
    args: dict[str, Any],
    task_id: str,
    stage: str,
    status: str,
    recorded: bool,
    stage_evidence: str = "",
) -> None:
    if not session_id:
        return
    try:
        from app.services.mcp_session_store import get_session_store

        checkpoint = {
            "project": str(args["project"]).strip(),
            "task_id": task_id,
            "stage": stage,
            "status": status,
            "summary": str(args["summary"]).strip(),
            "blockers": _string_list_arg(args.get("blockers")),
            "next_step": str(args.get("next_step") or "").strip(),
            "reason": str(args.get("reason") or "").strip(),
        }
        patch: dict[str, Any] = {
            "current_task_checkpoint": checkpoint,
            "task_checkpoint_recorded": recorded,
        }
        if recorded:
            checkpoint["recorded_at"] = time.time()
            checkpoint["stage_evidence"] = stage_evidence
            patch["task_checkpoint_recorded_at"] = time.time()
            patch["stage_evidence"] = stage_evidence
        await get_session_store().patch_context(session_id, patch)
    except Exception:
        pass


def _status_from_args(args: dict[str, Any], payload: dict[str, Any]) -> str:
    status_tag = next((tag for tag in payload.get("tags", []) if str(tag).startswith("task_status:")), "")
    status = str(args.get("status") or "").strip().lower()
    if not status and isinstance(status_tag, str) and ":" in status_tag:
        status = status_tag.split(":", 1)[1]
    return status or "active"


def _checkpoint_scope_tokens(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", str(text or "").lower())
        if token not in _CHECKPOINT_SCOPE_STOPWORDS
    }
    expanded: set[str] = set(tokens)
    for token in tokens:
        expanded.update(part for part in re.split(r"[-_]+", token) if len(part) > 2 and part not in _CHECKPOINT_SCOPE_STOPWORDS)
    return expanded


def _checkpoint_scope_text(args: dict[str, Any]) -> str:
    parts = [
        args.get("summary"),
        args.get("next_step"),
        args.get("reason"),
    ]
    for key in ("blockers", "decisions", "changed_files", "verification", "remaining_risk", "stage_evidence_refs", "write_scope"):
        value = args.get(key) or []
        if isinstance(value, str):
            parts.append(value)
        else:
            parts.extend(str(item) for item in value if str(item).strip())
    return "\n".join(str(part) for part in parts if str(part or "").strip())


def _string_list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []
