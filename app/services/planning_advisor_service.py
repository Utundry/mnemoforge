from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec


@lru_cache(maxsize=1)
def _advisor_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("planning/advisor.json")
    except Exception:
        return {"query_triggers": [], "framing_fields": [], "next_work_rules": [], "max_candidates": 5}


def _normalized_text(value: object) -> str:
    return re.sub(r"[_\-/\.]+", " ", str(value or "")).casefold().strip()


def is_planning_advisor_query(query: str) -> bool:
    text = _normalized_text(query)
    if not text:
        return False
    return any(str(trigger or "").casefold() in text for trigger in _advisor_spec().get("query_triggers") or [])


def _field_specs() -> dict[str, dict[str, Any]]:
    return {
        str(item.get("field") or "").strip(): item
        for item in (_advisor_spec().get("framing_fields") or [])
        if isinstance(item, dict) and str(item.get("field") or "").strip()
    }


def task_framing_gaps_from_context(task_context: dict[str, Any]) -> list[dict[str, Any]]:
    quality = task_context.get("task_statement_quality") if isinstance(task_context.get("task_statement_quality"), dict) else {}
    missing = [str(item).strip() for item in (quality.get("missing_artifacts") or []) if str(item).strip()]
    next_actions = task_context.get("next_actions") if isinstance(task_context.get("next_actions"), list) else []
    specs = _field_specs()
    gaps: list[dict[str, Any]] = []
    for field in missing:
        spec = specs.get(field) or {}
        gap = {
            "field": field,
            "severity": spec.get("severity") or "medium",
            "suggestions": spec.get("suggestions") or [],
        }
        matching_action = next(
            (
                item
                for item in next_actions
                if isinstance(item, dict)
                and field.replace("_", " ") in str(item.get("action") or item.get("rationale") or "").casefold()
            ),
            None,
        )
        if matching_action:
            gap["recommended_action"] = matching_action.get("action")
            gap["rationale"] = matching_action.get("rationale")
        gaps.append({key: value for key, value in gap.items() if value not in (None, "", [])})
    return gaps


def _candidate_from_artifact(item: dict[str, Any]) -> dict[str, Any]:
    artifact_key = str(item.get("artifact_key") or "").strip()
    item_type = str(item.get("type") or "artifact").strip() or "artifact"
    candidate = {
        "type": item_type,
        "ref": artifact_key,
        "task_id": item.get("task_id"),
        "title": item.get("title"),
        "status": item.get("status"),
        "why_next": item.get("match_reason") or _why_next_for_type(item_type),
        "recommended_next_call": _recommended_next_call(item),
    }
    return {key: value for key, value in candidate.items() if value not in (None, "", [], {})}


def _why_next_for_type(item_type: str) -> str:
    if item_type == "task":
        return "Open task is already promoted work."
    if item_type == "improvement":
        return "Open improvement is an embryonic task candidate."
    return "Open work artifact is available for review."


def _recommended_next_call(item: dict[str, Any]) -> dict[str, Any]:
    item_type = str(item.get("type") or "").strip()
    artifact_key = str(item.get("artifact_key") or "").strip()
    project = str(item.get("project") or "").strip()
    task_id = str(item.get("task_id") or "").strip()
    if item_type == "task" and task_id:
        payload = {"project": project, "task_id": task_id}
        return {"tool": "submit", "form_id": "get_task_context", "payload": {k: v for k, v in payload.items() if v}}
    if artifact_key:
        return {"tool": "get", "ref": artifact_key}
    return {}


def _rule_why(rule_id: str) -> str:
    for rule in _advisor_spec().get("next_work_rules") or []:
        if isinstance(rule, dict) and rule.get("id") == rule_id:
            return str(rule.get("why") or "")
    return ""


def build_next_work_advisor(
    artifact_data: dict[str, Any],
    *,
    project: str,
    query: str,
    limit: int | None = None,
) -> dict[str, Any]:
    del query
    items = [item for item in (artifact_data.get("items") or []) if isinstance(item, dict)]
    max_candidates = limit or int(_advisor_spec().get("max_candidates") or 5)
    tasks = [item for item in items if str(item.get("type") or "") == "task"]
    improvements = [item for item in items if str(item.get("type") or "") == "improvement"]
    if tasks:
        rule_id = "prefer_open_tasks"
        chosen = tasks + improvements
    elif improvements:
        rule_id = "promote_open_improvements"
        chosen = improvements
    else:
        rule_id = "request_new_improvement"
        chosen = []
    return {
        "status": "ready" if chosen else "empty",
        "project": project,
        "advisor": "planning_next_work",
        "selection_rule": rule_id,
        "why": _rule_why(rule_id),
        "next_work_candidates": [_candidate_from_artifact(item) for item in chosen[:max_candidates]],
        "next_safe_action": (
            "Review the first candidate with get/submit before claiming implementation work."
            if chosen
            else "Create or import an improvement before starting implementation work."
        ),
    }
