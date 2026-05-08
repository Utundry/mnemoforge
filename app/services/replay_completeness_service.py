from __future__ import annotations

from typing import Any

REPLAY_COMPLETENESS_RELEASE_GATE = "replay_completeness_v1"

REPLAY_REQUIRED_FIELDS = [
    "task.title",
    "task.status",
    "latest_checkpoint.stage",
    "latest_checkpoint.summary",
    "latest_checkpoint.changed_files",
    "latest_checkpoint.verification",
    "next_safe_action",
]

EXECUTION_READINESS_REQUIRED_EVIDENCE = [
    "linked_improvement",
    "decision_history",
    "implementation_history",
    "verification_evidence",
    "risk_evidence",
    "handoff_refs",
    "project_context_refs",
    "next_safe_action",
]

DEFAULT_MODEL_CONTEXT_WINDOW = 32_000
DEFAULT_RESUME_BUDGET_RATIO = 0.05
DEFAULT_RESUME_BUDGET_FLOOR = 800
DEFAULT_RESUME_BUDGET_HARD_CAP = 6_000
DEFAULT_RESUME_SOFT_OVERFLOW_RATIO = 0.15

RESUME_BUDGET_PROFILE_RATIOS = {
    "normal": 0.05,
    "complex": 0.08,
    "handoff": 0.08,
    "emergency": 0.10,
}


def estimate_tokens_from_chars(response_chars: int) -> int:
    return max(0, (max(0, int(response_chars)) + 3) // 4)


def build_token_budget(
    *,
    response_chars: int,
    model_context_window: int | None = None,
    resume_budget_ratio: float | None = None,
    resume_budget_profile: str = "normal",
    min_floor: int = DEFAULT_RESUME_BUDGET_FLOOR,
    hard_cap: int = DEFAULT_RESUME_BUDGET_HARD_CAP,
    soft_overflow_ratio: float = DEFAULT_RESUME_SOFT_OVERFLOW_RATIO,
    overflow_reason: str = "",
) -> dict[str, Any]:
    profile = str(resume_budget_profile or "normal").strip().lower() or "normal"
    context_window = max(1, int(model_context_window or DEFAULT_MODEL_CONTEXT_WINDOW))
    ratio = float(resume_budget_ratio) if resume_budget_ratio is not None else RESUME_BUDGET_PROFILE_RATIOS.get(profile, DEFAULT_RESUME_BUDGET_RATIO)
    ratio = max(0.001, min(ratio, 0.5))
    floor = max(1, int(min_floor))
    cap = max(floor, int(hard_cap))
    raw_budget = int(context_window * ratio)
    budget_tokens = max(floor, min(raw_budget, cap))
    estimated_tokens = estimate_tokens_from_chars(response_chars)
    soft_ratio = max(0.0, float(soft_overflow_ratio))
    soft_limit_tokens = int(budget_tokens * (1 + soft_ratio))
    overflow_tokens = max(0, estimated_tokens - budget_tokens)
    within_budget = estimated_tokens <= budget_tokens
    within_soft_limit = estimated_tokens <= soft_limit_tokens
    if overflow_tokens and not overflow_reason:
        overflow_reason = "Compact response preserves required replay and execution context."
    return {
        "basis": "model_context_window_ratio",
        "profile": profile,
        "context_window": context_window,
        "ratio": ratio,
        "min_floor": floor,
        "hard_cap": cap,
        "soft_overflow_ratio": soft_ratio,
        "budget_tokens": budget_tokens,
        "soft_limit_tokens": soft_limit_tokens,
        "response_chars": response_chars,
        "estimated_tokens": estimated_tokens,
        "within_budget": within_budget,
        "within_soft_limit": within_soft_limit,
        "overflow_tokens": overflow_tokens,
        "overflow_reason": overflow_reason if overflow_tokens else "",
    }


def evaluate_replay_completeness(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task") or {}
    checkpoint = payload.get("latest_checkpoint") or {}
    values = {
        "task.title": task.get("title"),
        "task.status": task.get("status"),
        "latest_checkpoint.stage": checkpoint.get("stage"),
        "latest_checkpoint.summary": checkpoint.get("summary"),
        "latest_checkpoint.changed_files": checkpoint.get("changed_files"),
        "latest_checkpoint.verification": checkpoint.get("verification"),
        "next_safe_action": payload.get("next_safe_action"),
    }
    missing = [
        field
        for field in REPLAY_REQUIRED_FIELDS
        if values.get(field) in (None, "", [])
    ]
    return {
        "status": "complete" if not missing else "incomplete",
        "required_fields": list(REPLAY_REQUIRED_FIELDS),
        "missing_fields": missing,
        "can_continue_without_user": not missing,
        "release_gate": REPLAY_COMPLETENESS_RELEASE_GATE,
    }


def _history_has_type(history: list[dict[str, Any]], change_type: str) -> bool:
    return any(str(item.get("change_type") or "") == change_type for item in history)


def _history_has_stage(history: list[dict[str, Any]], stage: str) -> bool:
    stage = stage.strip()
    if not stage:
        return False
    stage_tag = f"task_stage:{stage}"
    for item in history:
        if str(item.get("stage") or "") == stage:
            return True
        if stage_tag in (item.get("tags") or []):
            return True
        content = str(item.get("content") or "")
        if f"Checkpoint stage: {stage}" in content:
            return True
    return False


def evaluate_execution_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = payload.get("replay_bundle") or {}
    checkpoint = payload.get("latest_checkpoint") or {}
    history = bundle.get("task_history") or []
    linked_improvement = bundle.get("linked_improvement") or {}
    context_refs = bundle.get("project_context_refs") or {}

    evidence = {
        "linked_improvement": bool(linked_improvement.get("available")),
        "decision_history": _history_has_type(history, "decision") or bool(checkpoint.get("decisions")),
        "implementation_history": _history_has_type(history, "implementation") or _history_has_stage(history, "in_progress"),
        "verification_evidence": bool(checkpoint.get("verification")),
        "risk_evidence": bool(checkpoint.get("remaining_risk")),
        "handoff_refs": bool(bundle.get("handoff_refs")),
        "project_context_refs": bool(context_refs.get("readiness_tool") and context_refs.get("enrichment_tool")),
        "next_safe_action": bool(str(payload.get("next_safe_action") or "").strip()),
    }
    missing = [
        item
        for item in EXECUTION_READINESS_REQUIRED_EVIDENCE
        if not evidence.get(item)
    ]
    return {
        "status": "ready" if not missing else "incomplete",
        "required_evidence": list(EXECUTION_READINESS_REQUIRED_EVIDENCE),
        "missing_evidence": missing,
        "evidence": evidence,
        "can_choose_next_action_without_user": not missing,
        "recommended_next_tool": "continue_task" if not missing else "record_task_checkpoint",
        "recommended_next_action": payload.get("next_safe_action") if not missing else "Record a checkpoint with missing execution evidence.",
    }


def build_replay_drill_decision(payload: dict[str, Any]) -> dict[str, Any]:
    replay = payload.get("replay_completeness") or evaluate_replay_completeness(payload)
    execution = payload.get("execution_readiness") or evaluate_execution_readiness(payload)
    bundle = payload.get("replay_bundle") or {}
    context_refs = bundle.get("project_context_refs") or {}
    project_id = context_refs.get("project_id") or payload.get("project")
    task_id = context_refs.get("task_id") or payload.get("task_id")
    next_action = str(payload.get("next_safe_action") or "").strip()

    if replay.get("status") != "complete":
        return {
            "status": "blocked",
            "first_tool": "record_task_checkpoint",
            "first_action": "Record missing replay completeness fields before continuing.",
            "tool_arguments": {
                "project": project_id,
                "task_id": task_id,
                "stage": "handoff",
                "summary": "Replay completeness is incomplete.",
            },
            "rationale": "Replay completeness is incomplete.",
            "blocking_missing": replay.get("missing_fields") or [],
            "evidence_used": ["replay_completeness"],
        }

    if execution.get("status") != "ready":
        return {
            "status": "blocked",
            "first_tool": "record_task_checkpoint",
            "first_action": "Record missing execution evidence before continuing.",
            "tool_arguments": {
                "project": project_id,
                "task_id": task_id,
                "stage": "handoff",
                "summary": "Execution readiness is incomplete.",
            },
            "rationale": "Execution readiness is incomplete.",
            "blocking_missing": execution.get("missing_evidence") or [],
            "evidence_used": ["execution_readiness"],
        }

    first_tool = str(context_refs.get("enrichment_tool") or "continue_task").strip() or "continue_task"
    tool_arguments: dict[str, Any] = {}
    if first_tool == "enrich_task_with_context":
        tool_arguments = {
            "project_id": project_id,
            "task": next_action,
            "context_profile": "handoff_compact",
        }
    elif first_tool == "continue_task":
        tool_arguments = {
            "project": project_id,
            "task_id": task_id,
            "include_handoffs": True,
        }

    return {
        "status": "ready",
        "first_tool": first_tool,
        "first_action": next_action,
        "tool_arguments": tool_arguments,
        "rationale": "Replay and execution readiness are complete; use project context refs before executing the next safe action.",
        "blocking_missing": [],
        "evidence_used": [
            "replay_completeness",
            "execution_readiness",
            "replay_bundle.project_context_refs",
            "next_safe_action",
        ],
    }
