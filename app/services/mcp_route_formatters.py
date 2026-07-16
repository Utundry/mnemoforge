"""Formatting helpers for MCP facade route results."""
from __future__ import annotations

import json
import re
from typing import Any

from app.services.mcp_sse_tool_catalog import _extract_task_id_like_from_text


def facade_action_card(
    *,
    facade: str,
    route: dict[str, Any],
    args: dict[str, Any],
    executed: bool,
    warnings: list[str],
    guarded_tools: list[str] | None = None,
) -> dict[str, Any]:
    action_status = "executed" if executed else "needs_confirmation" if route.get("mutating") else "ready"
    recommended_next_call = None
    if not executed:
        recommended_next_call = {
            "tool": facade if route.get("mutating") else route["tool"],
            "arguments": ({**args, "allow_mutation": True} if route.get("mutating") else route["payload"]),
        }
    return {
        "action_status": action_status,
        "one_sentence_summary": (
            f"{facade} selected {route['tool']} for {route['intent_type']} "
            f"with confidence {route['confidence']:.2f}."
        ),
        "recommended_next_call": recommended_next_call,
        "confirmation_required": action_status == "needs_confirmation",
        "confirmation_phrase": "set allow_mutation=true after reviewing submit_payload" if action_status == "needs_confirmation" else "",
        "do_not_call": guarded_tools if action_status == "needs_confirmation" else [],
        "why": route.get("reason"),
        "warnings": warnings,
    }


def build_route_telemetry(
    *,
    facade: str,
    route: dict[str, Any],
    executed: bool,
    warnings: list[str],
    args: dict[str, Any],
) -> dict[str, Any]:
    scorer = route.get("scorer") if isinstance(route.get("scorer"), dict) else {}
    return {
        "facade": facade,
        "intent": str(args.get("intent") or "").strip()[:240],
        "intent_type": route.get("intent_type"),
        "underlying_tool": route.get("tool"),
        "mutating": bool(route.get("mutating")),
        "executed": bool(executed),
        "guardrail_triggered": bool(route.get("mutating")) and not bool(executed),
        "confidence": route.get("confidence"),
        "scorer_backend": scorer.get("backend_used") or scorer.get("backend_requested") or "rule_based",
        "fallback_used": bool(scorer.get("fallback_reason")) or route.get("tool") == "tool_recommend",
        "fallback_reason": scorer.get("fallback_reason") or "",
        "matched_pattern_id": scorer.get("matched_pattern_id") or scorer.get("learned_pattern_id") or "",
        "matched_pattern_score": scorer.get("matched_pattern_score"),
        "matched_by": scorer.get("matched_by") or "",
        "warnings": list(warnings),
        "reason": str(route.get("reason") or "").strip(),
        "project": route.get("payload", {}).get("project") or route.get("payload", {}).get("project_id") or args.get("project") or args.get("project_id") or "",
        "task_id": route.get("payload", {}).get("task_id") or args.get("task_id") or "",
    }


def selected_route_public(route: dict[str, Any]) -> dict[str, Any]:
    selected = {
        "tool": route["tool"],
        "intent_type": route["intent_type"],
        "mutating": bool(route.get("mutating")),
        "confidence": route["confidence"],
        "reason": route["reason"],
    }
    if route.get("matched_example"):
        selected["matched_example"] = route.get("matched_example")
    if route.get("route_candidates"):
        selected["route_candidates"] = route.get("route_candidates")
    if isinstance(route.get("scorer"), dict):
        selected["scorer"] = route.get("scorer")
    return selected


def wants_route_diagnostic(args: dict[str, Any]) -> bool:
    return bool(args.get("diagnostic")) or str(args.get("response_format") or "").strip().lower() == "diagnostic"


def wants_route_answer(args: dict[str, Any]) -> bool:
    return bool(args.get("answer")) or str(args.get("response_format") or "").strip().lower() == "answer"


def diagnostic_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:500]
    return str(value)


def first_route_diagnostic_task_id(result: Any, *, preferred_task_id: str = "") -> str:
    if isinstance(result, dict):
        if result.get("task_id"):
            return str(result["task_id"])
        receipt = result.get("receipt")
        if isinstance(receipt, dict) and receipt.get("task_id"):
            return str(receipt["task_id"])
        items = result.get("items")
        if isinstance(items, list) and items:
            preferred = str(preferred_task_id or "").strip().casefold()
            if preferred:
                for item in items:
                    if isinstance(item, dict) and str(item.get("task_id") or "").casefold().startswith(preferred):
                        return str(item["task_id"])
            first = items[0]
            if isinstance(first, dict) and first.get("task_id"):
                return str(first["task_id"])
    return ""


def first_route_result_item(result: Any, *, preferred_task_id: str = "") -> dict[str, Any]:
    if isinstance(result, dict):
        if any(key in result for key in ("task_id", "title", "status", "artifact_key")):
            return result
        receipt = result.get("receipt")
        if isinstance(receipt, dict) and any(key in receipt for key in ("task_id", "title", "status", "artifact_key")):
            return receipt
        items = result.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            preferred = str(preferred_task_id or "").strip().casefold()
            if preferred:
                for item in items:
                    if isinstance(item, dict) and str(item.get("task_id") or "").casefold().startswith(preferred):
                        return item
            return items[0]
    return {}


def format_route_diagnostic(data: dict[str, Any]) -> str:
    selected = data.get("selected_route") or {}
    scorer = selected.get("scorer") if isinstance(selected.get("scorer"), dict) else {}
    telemetry = data.get("route_telemetry") if isinstance(data.get("route_telemetry"), dict) else {}
    result = data.get("result")
    preferred_task_id = _extract_task_id_like_from_text(str(data.get("intent") or ""))
    executed = bool(data.get("executed"))
    guardrail_triggered = bool(selected.get("mutating")) and not executed
    lines = [
        "SloplessCode route diagnostic",
        f"facade={diagnostic_value(data.get('facade'))}",
        f"project={diagnostic_value(data.get('project'))}",
        f"intent={diagnostic_value(data.get('intent'))}",
        f"status={diagnostic_value(data.get('status'))}",
        f"action_status={diagnostic_value(data.get('action_status'))}",
        f"executed={diagnostic_value(executed)}",
        f"guardrail_triggered={diagnostic_value(guardrail_triggered)}",
        f"confirmation_required={diagnostic_value(guardrail_triggered)}",
        f"route.tool={diagnostic_value(selected.get('tool'))}",
        f"route.intent_type={diagnostic_value(selected.get('intent_type'))}",
        f"route.mutating={diagnostic_value(selected.get('mutating'))}",
        f"route.confidence={diagnostic_value(selected.get('confidence'))}",
        f"scorer.backend_requested={diagnostic_value(scorer.get('backend_requested'))}",
        f"scorer.backend_used={diagnostic_value(scorer.get('backend_used'))}",
        f"scorer.llm_attempted={diagnostic_value(scorer.get('llm_attempted'))}",
        f"scorer.fallback_reason={diagnostic_value(scorer.get('fallback_reason'))}",
        f"scorer.learned_pattern_id={diagnostic_value(scorer.get('learned_pattern_id'))}",
        f"scorer.matched_pattern_id={diagnostic_value(scorer.get('matched_pattern_id'))}",
        f"telemetry.scorer_backend={diagnostic_value(telemetry.get('scorer_backend'))}",
        f"telemetry.fallback_used={diagnostic_value(telemetry.get('fallback_used'))}",
        f"telemetry.fallback_reason={diagnostic_value(telemetry.get('fallback_reason'))}",
        f"telemetry.matched_pattern_id={diagnostic_value(telemetry.get('matched_pattern_id'))}",
        f"telemetry.matched_pattern_score={diagnostic_value(telemetry.get('matched_pattern_score'))}",
        f"telemetry.matched_by={diagnostic_value(telemetry.get('matched_by'))}",
        f"warnings={diagnostic_value(data.get('warnings') or telemetry.get('warnings') or [])}",
        f"first_task_id={diagnostic_value(first_route_diagnostic_task_id(result, preferred_task_id=preferred_task_id))}",
        f"next_safe_action={diagnostic_value(data.get('next_safe_action'))}",
    ]
    return "\n".join(lines)


def format_route_answer(data: dict[str, Any]) -> str:
    selected = data.get("selected_route") if isinstance(data.get("selected_route"), dict) else {}
    result = data.get("result")
    preferred_task_id = _extract_task_id_like_from_text(str(data.get("intent") or ""))
    first = first_route_result_item(result, preferred_task_id=preferred_task_id)
    intent_type = str(selected.get("intent_type") or "")
    lines = ["SloplessCode answer"]

    if selected.get("mutating") and not data.get("executed"):
        lines.append("Answer: No mutation was executed. Review the guarded route before allowing changes.")
        lines.append("executed=false")
        lines.append("mutation_executed=false")
        lines.append("confirmation_required=true")
        lines.append("do_not_claim_created=true")
    elif intent_type == "task_lookup":
        task_id = first.get("task_id") or first_route_diagnostic_task_id(result, preferred_task_id=preferred_task_id)
        if task_id:
            lines.append(f"Answer: Found task {task_id}.")
        else:
            lines.append("Answer: No exact task was found in the first result.")
    elif data.get("facade") == "project_work" and intent_type == "next_priority":
        title = str(first.get("title") or "").strip()
        if title:
            lines.append(f"Answer: Next useful project action is {title}.")
        else:
            lines.append("Answer: No open project task was found.")
    elif intent_type == "project_readiness":
        lines.append("Answer: Project readiness route executed.")
    elif data.get("executed"):
        lines.append(f"Answer: {data.get('facade') or 'facade'} executed route {selected.get('tool') or ''}.")
    else:
        lines.append("Answer: Route was selected but not executed.")

    if isinstance(result, str) and result.strip() and not first:
        compact_result = re.sub(r"\s+", " ", result).strip()
        lines.append(f"result={compact_result[:900]}")
    if first.get("task_id"):
        lines.append(f"task_id={diagnostic_value(first.get('task_id'))}")
    if first.get("title"):
        lines.append(f"title={diagnostic_value(first.get('title'))}")
    if first.get("status"):
        lines.append(f"task_status={diagnostic_value(first.get('status'))}")
    if first.get("artifact_key"):
        lines.append(f"artifact_key={diagnostic_value(first.get('artifact_key'))}")
    if first.get("matched_topic_tags"):
        lines.append(f"matched_topic_tags={diagnostic_value(first.get('matched_topic_tags'))}")
    if first.get("match_reason"):
        lines.append(f"why_match={diagnostic_value(first.get('match_reason'))}")
    if first.get("work_token"):
        lines.append(f"work_token={diagnostic_value(first.get('work_token'))}")
    if first.get("lease_id"):
        lines.append(f"lease_id={diagnostic_value(first.get('lease_id'))}")
    if first.get("work_session_id"):
        lines.append(f"work_session_id={diagnostic_value(first.get('work_session_id'))}")
    if data.get("facade") == "project_work" and intent_type == "next_priority":
        lines.append(f"why={diagnostic_value(selected.get('reason'))}")
    if data.get("warnings"):
        lines.append(f"warnings={diagnostic_value(data.get('warnings'))}")
    if not data.get("executed") and data.get("next_safe_action"):
        lines.append(f"next_safe_action={diagnostic_value(data.get('next_safe_action'))}")
    return "\n".join(lines)
