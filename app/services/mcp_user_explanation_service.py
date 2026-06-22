from __future__ import annotations

from typing import Any


def user_explanation_for_task(result: dict[str, Any], *, state: str = "planning") -> str:
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    quality = result.get("task_statement_quality") if isinstance(result.get("task_statement_quality"), dict) else {}
    readiness = result.get("execution_readiness") if isinstance(result.get("execution_readiness"), dict) else {}
    missing = {str(item or "").strip() for item in (quality.get("missing_artifacts") or []) if str(item or "").strip()}
    status = str(result.get("status") or task.get("status") or "").strip().lower()
    if status == "done":
        return "This task is completed; use it as history or evidence, not as new implementation work."
    if "definition_of_done" in missing:
        return "This task is not ready for implementation: complete the missing Definition of Done and get explicit approval first."
    if str(readiness.get("status") or "").strip().lower() == "incomplete":
        return "This task context is incomplete; follow next_safe_action before starting implementation."
    if task.get("linked_improvement_id") or result.get("linked_improvement_id"):
        return "This task is linked to an improvement; confirm the current framing before implementation."
    return "This is a task context packet; use next_safe_action and approval fields before changing the project."


def user_explanation_for_artifact(item: dict[str, Any], *, kind: str = "") -> str:
    item_type = str(kind or item.get("type") or "artifact").strip().lower()
    status = str(item.get("status") or "").strip().lower()
    linked = str(item.get("linked_artifact_key") or "").strip()
    if item_type == "improvement":
        if status in {"done", "resolved"}:
            return "This improvement is resolved; treat it as completed backlog history."
        return "This improvement is an embryonic task candidate; review it and complete task framing before implementation."
    if item_type == "task":
        if status == "done":
            return "This task is completed; use it as history or evidence, not as new implementation work."
        if item.get("task_statement_incomplete") or linked.startswith("improvement:"):
            return "This task may be a projection of an improvement; verify the full task statement before claiming it."
        return "This is an open task candidate; inspect task context and get explicit approval before implementation."
    return "This is a public artifact result; use next_safe_action or inspect the referenced artifact before acting."