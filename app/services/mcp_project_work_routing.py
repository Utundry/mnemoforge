from __future__ import annotations

import re
from typing import Any

from app.services.intent_polarity import analyze_intent_polarity
from app.services.mcp_workflow_specs import load_route_catalog_spec

PROJECT_WORK_ROUTE_CATALOG: tuple[dict[str, Any], ...] = tuple(
    route.model_dump()
    for route in load_route_catalog_spec("project_work").routes
)


def project_work_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    intent = str(args.get("intent") or "").strip()
    text = " ".join(
        part
        for part in (
            intent,
            str(args.get("summary") or "").strip(),
            str(args.get("raw_notes") or "").strip(),
            str(args.get("state") or "").strip(),
        )
        if part
    )
    task_id = str(args.get("task_id") or "").strip()
    artifact_key = str(args.get("artifact_key") or "").strip()
    changed_files = _string_list_arg(args.get("changed_files"))
    verification = _string_list_arg(args.get("verification"))
    evidence: list[str] = ["facade:project_work", f"project:{project}"]
    if task_id:
        evidence.append(f"task_id:{task_id}")
    if artifact_key:
        evidence.append(f"artifact_key:{artifact_key}")
    if changed_files:
        evidence.append("changed_files_present")
    if verification:
        evidence.append("verification_present")

    route = {
        "family": "project_knowledge",
        "tool": "tool_recommend",
        "intent_type": "ambiguous",
        "confidence": 0.55,
        "mutating": False,
        "payload": {"task": intent, "project_id": project, "top_n": 3},
        "reason": "Intent is ambiguous; use tool recommendation rather than guessing a specialized route.",
        "matched_example": "",
        "route_candidates": [],
        "scorer": scorer_meta or {
            "backend_requested": str(args.get("scorer_backend") or "auto"),
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
    }

    catalog_route, route_candidates = _selected_catalog_route(text, args)
    route["route_candidates"] = route_candidates
    if llm_decision and _catalog_route_by_intent(str(llm_decision.get("intent_type") or "")):
        chosen = _catalog_route_by_intent(str(llm_decision.get("intent_type") or ""))
        assert chosen is not None
        catalog_route = chosen
        route_candidates = [
            {
                "intent_type": chosen["intent_type"],
                "tool": chosen["tool"],
                "score": round(float(llm_decision.get("confidence") or 0.0), 3),
                "matched_example": str(llm_decision.get("matched_example") or "llm_disambiguation"),
                "scorer": "llm",
            },
            *[item for item in route["route_candidates"] if item.get("intent_type") != chosen["intent_type"]],
        ][:3]
        route["route_candidates"] = route_candidates
    if catalog_route:
        best_score = route_candidates[0]["score"] if route_candidates else 0.0
        route.update(
            {
                "family": catalog_route["family"],
                "tool": catalog_route["tool"],
                "intent_type": catalog_route["intent_type"],
                "confidence": round(min(0.95, 0.55 + best_score * 0.4), 2),
                "mutating": catalog_route["mutating"],
                "reason": catalog_route["reason"],
                "matched_example": route_candidates[0].get("matched_example", "") if route_candidates else "",
            }
        )
    route = _apply_payload(route, args, text)
    route["reason"] = _finalize_route_reason(route)
    route["evidence"] = evidence
    if route.get("matched_example"):
        route["evidence"].append(f"matched_example:{route['matched_example']}")
    return route


def project_work_needs_llm_disambiguation(candidates: list[dict[str, Any]]) -> bool:
    if not candidates:
        return True
    top = float(candidates[0].get("score") or 0.0)
    second = float(candidates[1].get("score") or 0.0) if len(candidates) > 1 else 0.0
    return top < 0.34 or (top - second) < 0.08


def _selected_catalog_route(text: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = _catalog_scores(text, args)
    if not candidates or candidates[0]["score"] < 0.22:
        return None, candidates[:3]
    best = candidates[0]
    route = next(item for item in PROJECT_WORK_ROUTE_CATALOG if item["intent_type"] == best["intent_type"])
    return route, candidates[:3]


def _catalog_route_by_intent(intent_type: str) -> dict[str, Any] | None:
    clean = str(intent_type or "").strip()
    return next((item for item in PROJECT_WORK_ROUTE_CATALOG if item["intent_type"] == clean), None)


def _catalog_scores(text: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    lowered = str(text or "").casefold()
    intent_tokens = _route_tokens(lowered)
    has_create_task_intent = _has_create_task_intent(lowered, intent_tokens)
    candidates: list[dict[str, Any]] = []
    for route in PROJECT_WORK_ROUTE_CATALOG:
        best_score = 0.0
        best_example = ""
        for example in route["examples"]:
            example_text = str(example).casefold()
            example_tokens = _route_tokens(example_text)
            if not example_tokens:
                continue
            overlap = len(intent_tokens & example_tokens)
            union = len(intent_tokens | example_tokens) or 1
            score = overlap / union
            if example_text in lowered:
                score += 0.45
            elif all(token in intent_tokens for token in example_tokens):
                score += 0.25
            if score > best_score:
                best_score = score
                best_example = example

        if route["intent_type"] == "pull_task_context" and args.get("task_id"):
            best_score += 0.05
        if route["intent_type"] in {"start_task_session", "finish_task_session"} and args.get("task_id"):
            best_score += 0.08
        if route["intent_type"] == "start_task_session" and intent_tokens & {"start", "begin", "claim", "take"}:
            best_score += 0.12
        if route["intent_type"] == "finish_task_session" and intent_tokens & {"finish", "complete", "end", "release", "close"}:
            best_score += 0.14
        if route["intent_type"] == "verify_or_live_validate" and args.get("changed_files"):
            best_score += 0.05
        if route["intent_type"] == "capture_or_closeout" and (args.get("summary") or args.get("raw_notes")):
            best_score += 0.04
        if route["intent_type"] == "rule_work" and intent_tokens & {"rule", "law", "candidate"}:
            best_score += 0.08
        if route["intent_type"] == "review_task_capture" and intent_tokens & {"capture", "draft", "drafts", "candidate", "candidates", "framing"}:
            best_score += 0.08
        if route["intent_type"] == "approve_checkpoint_draft" and intent_tokens & {"approve", "approved", "accept", "save", "persist"}:
            best_score += 0.2
        if route["intent_type"] == "reject_checkpoint_draft" and intent_tokens & {"reject", "rejected", "decline", "discard"}:
            best_score += 0.2
        if route["intent_type"] == "create_task":
            best_score = best_score + 0.5 if has_create_task_intent else 0.0
        candidates.append(
            {
                "intent_type": route["intent_type"],
                "tool": route["tool"],
                "score": round(min(best_score, 1.0), 3),
                "matched_example": best_example,
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["intent_type"]))
    return candidates


def _apply_payload(route: dict[str, Any], args: dict[str, Any], text: str) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    intent = str(args.get("intent") or "").strip()
    task_id = str(args.get("task_id") or "").strip()
    artifact_key = str(args.get("artifact_key") or "").strip()
    changed_files = _string_list_arg(args.get("changed_files"))
    verification = _string_list_arg(args.get("verification"))
    limit = max(1, min(100, int(args.get("limit") or 10)))
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    danger_mode, danger_confirmation = _danger_bypass_args(args, intent)

    if route["intent_type"] == "next_priority":
        claim_filter, filter_resolution = _resolve_claim_filter(
            args=args,
            text=text,
            default="available",
        )
        assignment_filter = str(args.get("assignment_filter") or "").strip().lower()
        if assignment_filter not in {"all", "independent", "needs_review"}:
            assignment_filter = "independent" if _route_tokens(text) & {"multi", "agent", "agents", "parallel", "another", "delegate", "handoff"} else "all"
        route["payload"] = {
            "project": project,
            "limit": limit,
            "claim_filter": claim_filter,
            "include_claims": True,
            "assignment_filter": assignment_filter,
        }
        route["claim_filter_resolution"] = filter_resolution
    elif route["intent_type"] == "list_all_tasks":
        claim_filter, filter_resolution = _resolve_claim_filter(
            args=args,
            text=text,
            default="all",
        )
        artifact_type = str(args.get("artifact_type") or args.get("type") or "all").strip().lower()
        if artifact_type not in {"all", "task", "improvement"}:
            artifact_type = "all"
        route["payload"] = {
            "project": project,
            "limit": limit,
            "claim_filter": claim_filter,
            "include_claims": True,
            "assignment_filter": "all",
            "artifact_type": artifact_type,
        }
        route["claim_filter_resolution"] = filter_resolution
    elif route["intent_type"] == "pull_task_context":
        route["payload"] = {
            "project": project,
            "task_id": task_id,
            "detail": detail,
            "include_handoffs": True,
            "limit": limit,
        }
    elif route["intent_type"] == "capture_or_closeout":
        summary = str(args.get("summary") or args.get("raw_notes") or intent).strip()
        tokens = _route_tokens(text)
        route["payload"] = {
            "project": project,
            "task_id": task_id,
            "artifact_key": artifact_key,
            "summary": summary or "Record project work result.",
            "changed_files": changed_files,
            "verification": verification,
            "stage": "completed" if tokens & {"close", "done", "completed"} else "in_progress",
            "checkpoint_mode": "standard",
            "next_step_scope": "operator_review" if tokens & {"close", "tail"} else "unknown",
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
            "session_id": str(args.get("session_id") or "").strip(),
            "work_token": str(args.get("work_token") or "").strip(),
            "source": "project_work",
        }
    elif route["intent_type"] == "create_task":
        summary = str(args.get("summary") or args.get("raw_notes") or intent).strip()
        route["payload"] = {
            "project": project,
            "task_id": "",
            "artifact_key": "",
            "title": _title_from_text(summary or intent, fallback="New project improvement"),
            "summary": summary or "Create a new project improvement task.",
            "changed_files": changed_files,
            "verification": verification,
            "stage": "planning",
            "checkpoint_mode": "lightweight",
            "next_step_scope": "follow_up_task",
            "create_issue_if_unmatched": True,
            "skip_auto_task_match": True,
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
            "source": "project_work:create_task",
        }
    elif route["intent_type"] == "verify_or_live_validate":
        tokens = _route_tokens(text)
        state = "live_validation" if tokens & {"restart", "health", "live"} else "verification"
        route["payload"] = {
            "project": project,
            "task_id": task_id,
            "task": intent,
            "state": state,
            "intent": intent,
            "changed_files": changed_files,
            "include_rules": True,
            "include_tools": True,
        }
    elif route["intent_type"] == "review_task_capture":
        route["payload"] = {"project": project, "task_id": task_id, "limit": limit}
    elif route["intent_type"] in {"approve_checkpoint_draft", "reject_checkpoint_draft"}:
        route["payload"] = {
            "draft_id": str(args.get("draft_id") or "").strip(),
            "version": int(args.get("version") or 1),
            "approved_by": str(args.get("approved_by") or args.get("acted_by") or "codex").strip() or "codex",
            "rejected_by": str(args.get("rejected_by") or args.get("acted_by") or "codex").strip() or "codex",
            "reason": str(args.get("reason") or "").strip(),
        }
    elif route["intent_type"] == "start_task_session":
        route["payload"] = {
            "project": project,
            "task_id": task_id,
            "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
            "session_id": str(args.get("session_id") or "").strip(),
            "danger_mode": danger_mode,
            "danger_confirmation": danger_confirmation,
            "lease_ttl_seconds": int(args.get("lease_ttl_seconds") or 900),
            "summary": str(args.get("summary") or intent or "Task claimed; work session started.").strip(),
            "reason": str(args.get("reason") or "project_work:start_task_session").strip(),
            "source": "project_work",
        }
    elif route["intent_type"] == "finish_task_session":
        route["payload"] = {
            "project": project,
            "task_id": task_id,
            "work_id": str(args.get("work_id") or "").strip(),
            "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
            "owner_agent": str(args.get("owner_agent") or args.get("agent_id") or "codex").strip() or "codex",
            "acted_by": str(args.get("acted_by") or args.get("agent_id") or "codex").strip() or "codex",
            "session_id": str(args.get("session_id") or "").strip(),
            "status": str(args.get("status") or "completed").strip(),
            "summary": str(args.get("summary") or intent or "Task session finished.").strip(),
            "verification": verification,
            "changed_files": changed_files,
            "next_step": str(args.get("next_step") or "").strip(),
            "checkpoint_mode": str(args.get("checkpoint_mode") or "standard").strip(),
            "work_token": str(args.get("work_token") or "").strip(),
            "danger_mode": danger_mode,
            "danger_confirmation": danger_confirmation,
            "reason": str(args.get("reason") or "project_work:finish_task_session").strip(),
            "source": "project_work",
        }
    elif route["intent_type"] == "rule_work":
        route["payload"] = {
            "project": project,
            "intent": intent,
            "suggested_first_tools": ["list_project_laws", "list_rule_candidates", "get_rule_candidate_review_packet"],
        }
    return route


def _resolve_claim_filter(
    *,
    args: dict[str, Any],
    text: str,
    default: str,
) -> tuple[str, dict[str, Any]]:
    explicit = str(args.get("claim_filter") or "").strip().lower()
    if explicit in {"available", "claimed", "all"}:
        return explicit, {
            "value": explicit,
            "source": "explicit_argument",
            "reason": "A valid structured claim_filter argument takes precedence over natural-language inference.",
        }

    polarity = analyze_intent_polarity(
        text,
        signals={
            "available": ("available", "unclaimed", "free", "unoccupied"),
            "claimed": ("claimed", "occupied", "busy", "leased"),
            "all": ("all", "every", "both"),
        },
    )
    available_signal = "available" in polarity.positive or "claimed" in polarity.negative
    claimed_signal = "claimed" in polarity.positive or "available" in polarity.negative
    all_signal = "all" in polarity.positive
    if claimed_signal and available_signal:
        value = "all"
        reason = "Both claimed and available task groups were requested; include both groups."
    elif claimed_signal:
        value = "claimed"
        reason = "The request explicitly targets claimed, occupied, busy, or leased tasks."
    elif available_signal:
        value = "available"
        reason = "The request explicitly targets available/unclaimed tasks or negates claimed tasks."
    elif all_signal:
        value = "all"
        reason = "The request explicitly asks for all task groups."
    else:
        value = default
        reason = f"No lease-state preference was found; use the route default '{default}'."

    return value, {
        "value": value,
        "source": "natural_language" if value != default or available_signal or claimed_signal or all_signal else "route_default",
        "available_signal": available_signal,
        "claimed_signal": claimed_signal,
        "all_signal": all_signal,
        "polarity": polarity.evidence(),
        "reason": reason,
    }


def _finalize_route_reason(route: dict[str, Any]) -> str:
    base_reason = str(route.get("reason") or "").strip()
    payload = route.get("payload")
    resolution = route.get("claim_filter_resolution")
    if not isinstance(payload, dict) or not isinstance(resolution, dict):
        return base_reason

    claim_filter = str(payload.get("claim_filter") or "").strip().lower()
    if claim_filter not in {"available", "claimed", "all"}:
        return base_reason

    resolution_reason = str(resolution.get("reason") or "").strip()
    resolved = f"Final claim_filter={claim_filter}."
    if resolution_reason:
        resolved = f"{resolved} {resolution_reason}"
    return f"{base_reason} {resolved}".strip()


def _danger_bypass_args(args: dict[str, Any], intent: str) -> tuple[bool, str]:
    lowered_intent = str(intent or "").casefold()
    danger_mode = bool(args.get("danger_mode", False))
    danger_confirmation = str(args.get("danger_confirmation") or "")
    if "danger_mode=true" in lowered_intent or ("danger_mode" in lowered_intent and "true" in lowered_intent):
        danger_mode = True
    if "danger_confirmation=authorize_session_bypass" in lowered_intent or "authorize_session_bypass" in lowered_intent:
        danger_confirmation = "authorize_session_bypass"
    return danger_mode, danger_confirmation


def _has_create_task_intent(text: str, tokens: set[str]) -> bool:
    lowered = str(text or "").casefold()
    checkpoint_terms = {"checkpoint", "closeout", "handoff", "result", "progress"}
    explicit_markers = (
        "create a task",
        "create task",
        "create improvement",
        "save this as an improvement",
        "add this to backlog",
        "capture this as future work",
    )
    if tokens & checkpoint_terms and not any(marker in lowered for marker in explicit_markers):
        return False
    if any(
        marker in lowered
        for marker in (
            "create task",
            "create a task",
            "create improvement",
            "save improvement",
            "save this as an improvement",
            "add to backlog",
            "add this to backlog",
            "record new issue",
            "formulate task",
            "formulate this task",
            "capture this as future work",
        )
    ):
        return True
    return bool(tokens & {"create", "save", "record", "add", "formulate", "capture"}) and bool(
        tokens & {"task", "issue", "improvement", "backlog", "future", "work"}
    )


def _title_from_text(text: str, *, fallback: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if not clean:
        return fallback
    clean = re.sub(r"^(create|save|record|add|formulate|capture)\s+", "", clean, flags=re.IGNORECASE).strip(" :-")
    return (clean or fallback)[:96]


def _route_tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[\w]+", str(text or "").casefold(), flags=re.UNICODE) if len(token) >= 3}


def _string_list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []
