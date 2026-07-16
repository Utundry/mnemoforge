"""Compact result shaping for project_work facade responses."""
from __future__ import annotations

from typing import Any

from app.services.mcp_artifact_refs import artifact_type_from_key, task_id_from_artifact_key
from app.services.planning_advisor_service import collaborative_control_packet


def compact_project_work_result(route: dict[str, Any], result: Any) -> Any:
    if route.get("tool") == "list_open_tasks" and isinstance(result, dict):
        items = result.get("items") or []
        control = collaborative_control_packet()
        approval_intent = str(control.get("approval_intent") or "").strip()
        autonomous_override = str(control.get("autonomous_override") or "").strip()
        claim_after = " or ".join(part for part in (approval_intent, autonomous_override) if part)
        compact_items: list[dict[str, Any]] = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip() or artifact_type_from_key(item.get("artifact_key")) or "task"
            linked_task_id = task_id_from_artifact_key(item.get("linked_artifact_key"))
            compact_item = {
                "artifact_key": item.get("artifact_key"),
                "type": item_type,
                "title": item.get("title"),
                "status": item.get("status"),
                "task_id": item.get("task_id"),
                "linked_artifact_key": item.get("linked_artifact_key"),
                "linked_status": item.get("linked_status"),
                "work_priority": item.get("work_priority"),
                "lifecycle_stage": item.get("lifecycle_stage"),
                "implementation_ready": item.get("implementation_ready"),
                "claim_allowed": item.get("claim_allowed"),
            }
            if control:
                compact_item["framing_required"] = control.get("framing_required")
                compact_item["approval_required_before_claim"] = control.get("approval_required_before_claim")
                compact_item["approval_intent"] = control.get("approval_intent")
                compact_item["claim_after"] = claim_after
            if linked_task_id:
                compact_item["linked_task_id"] = linked_task_id
            if item_type == "improvement" and not compact_item.get("task_id"):
                compact_item["next_action"] = "Review the improvement and complete task framing before implementation approval."
            if compact_item.get("task_id"):
                compact_item["next_detail_form"] = {
                    "tool": control.get("review_first_tool") or "mailbox_submit",
                    "form_id": "get_task_context",
                    "payload": {
                        "project": item.get("project") or result.get("project"),
                        "task_id": compact_item["task_id"],
                        "detail": "compact",
                    },
                }
            elif linked_task_id:
                compact_item["next_detail_form"] = {
                    "tool": control.get("review_first_tool") or "mailbox_submit",
                    "form_id": "get_task_context",
                    "payload": {
                        "project": item.get("project") or result.get("project"),
                        "task_id": linked_task_id,
                        "detail": "compact",
                    },
                }
            if isinstance(item.get("task_claim"), dict):
                compact_item["claim_status"] = item.get("claim_status")
                compact_item["claimed_by"] = (item.get("task_claim") or {}).get("owner_agent")
            compact_items.append({key: value for key, value in compact_item.items() if value not in (None, "", [])})
        return compact_items
    if route.get("tool") == "pull_task_context" and isinstance(result, dict):
        return {
            "task_id": result.get("task_id"),
            "status": result.get("status"),
            "latest_checkpoint": result.get("latest_checkpoint"),
            "next_safe_action": result.get("next_safe_action"),
            "execution_readiness": result.get("execution_readiness"),
            "stenography_coverage": result.get("stenography_coverage"),
            "recommended_first_tool": result.get("recommended_first_tool"),
        }
    if route.get("tool") == "get_task_execution_context" and isinstance(result, dict):
        return {
            "state": result.get("state"),
            "readiness": result.get("readiness"),
            "required_rules": result.get("required_rules"),
            "recommended_rules": result.get("recommended_rules"),
            "risk_controls": result.get("risk_controls"),
            "next_transitions": result.get("next_transitions"),
        }
    if route.get("tool") == "get_project_readiness" and isinstance(result, dict):
        return {
            "project_id": result.get("project_id"),
            "readiness_level": result.get("readiness_level"),
            "readiness_score": result.get("readiness_score"),
            "summary": result.get("summary"),
            "blocking_gaps": (result.get("blocking_gaps") or [])[:5],
            "recommended_actions": (result.get("recommended_actions") or [])[:6],
            "next_safe_action": "Review project readiness; if memory is empty or partial, follow the recommended bootstrap actions before task work.",
        }
    if route.get("tool") == "task_capture_review" and isinstance(result, dict):
        return {
            "found": result.get("found"),
            "candidates": [
                {
                    "artifact_id": item.get("artifact_id"),
                    "kind": item.get("kind"),
                    "content": item.get("content"),
                    "confidence": item.get("confidence"),
                }
                for item in (result.get("candidates") or [])[:5]
                if isinstance(item, dict)
            ],
        }
    if route.get("tool") == "start_task_session" and isinstance(result, dict):
        return {
            "status": result.get("status"),
            "task_id": result.get("task_id"),
            "work_handle": result.get("work_handle"),
            "auto_heartbeat": result.get("auto_heartbeat"),
            "next_safe_action": "Use work_handle for checkpoints, finish, or recovery while the claim is active.",
        }
    if route.get("tool") == "mailbox_submit" and isinstance(result, dict):
        receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
        return {
            "status": receipt.get("status"),
            "form_id": receipt.get("form_id"),
            "improvement_id": receipt.get("id"),
            "artifact_key": receipt.get("artifact_key"),
            "task_id": receipt.get("task_id"),
            "canonical_task_id": receipt.get("canonical_task_id"),
            "task_artifact_key": receipt.get("task_artifact_key"),
            "linked_artifact_key": receipt.get("linked_artifact_key"),
            "task_status": receipt.get("task_status"),
            "authority_layer": receipt.get("authority_layer"),
            "classification_reason": receipt.get("classification_reason"),
            "matched_law_ref": receipt.get("matched_law_ref"),
            "matched_law_title": receipt.get("matched_law_title"),
            "matched_law_status": receipt.get("matched_law_status"),
            "canonical_status": receipt.get("canonical_status"),
            "created_task": receipt.get("created_task"),
            "idempotent_reuse": receipt.get("idempotent_reuse"),
            "suppress_improvement": receipt.get("suppress_improvement"),
            "lifecycle_stage": receipt.get("lifecycle_stage"),
            "implementation_ready": receipt.get("implementation_ready"),
            "claim_allowed": receipt.get("claim_allowed"),
            "framing_required": receipt.get("framing_required"),
            "next_safe_action": receipt.get("next_safe_action") or result.get("next_safe_action"),
        }
    return result

def project_work_action_card(
    *,
    route: dict[str, Any],
    executed: bool,
    result: Any,
    warnings: list[str],
    args: dict[str, Any],
) -> dict[str, Any]:
    if executed:
        action_status = "executed"
        recommended_next_call = None
    elif route.get("mutating"):
        action_status = "needs_confirmation"
        recommended_next_call = {
            "tool": "project_work",
            "arguments": {
                **{key: value for key, value in args.items() if key not in {"allow_mutation"}},
                "allow_mutation": True,
            },
        }
    elif route.get("tool") == "tool_recommend":
        action_status = "needs_clarification"
        recommended_next_call = {"tool": "tool_recommend", "arguments": route["payload"]}
    else:
        action_status = "ready"
        recommended_next_call = {"tool": route["tool"], "arguments": route["payload"]}

    one_sentence_summary = (
        f"project_work selected {route['tool']} for {route['intent_type']} "
        f"with confidence {route['confidence']:.2f}."
    )
    if executed:
        if isinstance(result, dict) and str(result.get("status") or "") == "conflict":
            one_sentence_summary += " The route hit a guardrail; follow the returned recovery protocol."
        else:
            one_sentence_summary += " The safe route was executed."
    elif route.get("mutating"):
        one_sentence_summary += " The route is guarded and needs explicit mutation confirmation."

    do_not_call = []
    if route.get("tool") in {"record_work_result", "record_task_checkpoint"} and not executed:
        do_not_call = ["record_task_checkpoint", "record_work_result"]
    elif route.get("tool") == "mailbox_submit" and not executed:
        do_not_call = ["mailbox_submit", "submit"]
    elif route.get("tool") == "project_rules":
        do_not_call = ["promote_rule_candidate", "revise_law_from_rule_candidate"]

    if executed and isinstance(result, dict) and str(result.get("status") or "") == "conflict":
        recommended_next_call = result.get("recommended_next_call") if isinstance(result.get("recommended_next_call"), dict) else None

    action_card = {
        "action_status": action_status,
        "one_sentence_summary": one_sentence_summary,
        "recommended_next_call": recommended_next_call,
        "confirmation_required": action_status == "needs_confirmation",
        "confirmation_phrase": "set allow_mutation=true after reviewing submit_payload" if action_status == "needs_confirmation" else "",
        "do_not_call": do_not_call,
        "why": route.get("reason"),
        "compact_result": compact_project_work_result(route, result),
        "warnings": warnings,
    }
    if route.get("tool") == "list_open_tasks":
        control = collaborative_control_packet()
        if control:
            action_card["collaborative_control"] = control
    return action_card


def weak_model_mutation_guardrail(route: dict[str, Any], executed: bool, action_card: dict[str, Any]) -> dict[str, Any] | None:
    if executed or not route.get("mutating"):
        return None
    return {
        "mutation_executed": False,
        "confirmation_required": True,
        "state_change": "not_executed",
        "do_not_claim_created": True,
        "plain_instruction": (
            "No task, checkpoint, rule, or artifact was created or changed. "
            "Say that only a guarded route plan was returned. Execute only after "
            "reviewing submit_payload and calling the recommended_next_call with allow_mutation=true."
        ),
        "recommended_next_call": action_card.get("recommended_next_call"),
    }

