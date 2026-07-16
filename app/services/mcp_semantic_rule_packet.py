from __future__ import annotations

import re
import time
from typing import Any, Callable

from app.services.mcp_workflow_specs import load_named_json_spec
from app.services.operational_instincts_service import get_active_operational_instincts


def _semantic_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9_]+", " ", str(text or "").lower())
    return {token for token in cleaned.split() if len(token) >= 3}


def _semantic_verification_spec() -> dict[str, Any]:
    spec = load_named_json_spec("workflow/semantic_verification.json")
    value = spec.get("declared_contour_precondition")
    return value if isinstance(value, dict) else {}


def _semantic_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip().lower() for item in value if str(item or "").strip()]


def _semantic_recovery_packet(*, policy: dict[str, Any], project: str, task_id: str, changed_files: Any) -> dict[str, Any]:
    recovery = policy.get("recovery") if isinstance(policy.get("recovery"), dict) else {}
    template = str(recovery.get("verification_contour_ref_template") or "").strip()
    verification_contour_ref = template.format(project=project, task_id=task_id) if template else ""
    call_spec = recovery.get("recommended_next_call") if isinstance(recovery.get("recommended_next_call"), dict) else {}
    return {
        "verification_contour_ref": verification_contour_ref,
        "forbidden_patterns": [str(item or "").strip() for item in recovery.get("forbidden_patterns") or [] if str(item or "").strip()],
        "recommended_next_call": {
            "tool": str(call_spec.get("tool") or "").strip(),
            "arguments": {
                "project": project,
                "task_id": task_id,
                "state": str(call_spec.get("state") or "verification").strip() or "verification",
                "intent": str(call_spec.get("intent") or "").strip(),
                "changed_files": list(changed_files or []),
            },
        },
        "next_safe_action": str(recovery.get("next_safe_action") or "").strip(),
    }


def _semantic_rules_context_type(*, facade: str, route: dict[str, Any], args: dict[str, Any]) -> str:
    if facade == "project_verify":
        intent = str(route.get("intent_type") or "").strip()
        if intent in {"verification_context", "restart_validation_plan"}:
            return "post_implementation"
        return "post_validation"
    if facade == "project_work":
        if bool(route.get("mutating")):
            return "pre_implementation"
        if str(route.get("intent_type") or "") in {"pull_task_context", "task_lookup"}:
            return "task_framing"
        return "option_selection"
    return "task_enrichment"


def build_semantic_rule_packet(
    *,
    facade: str,
    route: dict[str, Any],
    args: dict[str, Any],
    format_error: Callable[[Exception], str],
) -> dict[str, Any]:
    payload = route.get("payload") if isinstance(route.get("payload"), dict) else {}
    project = str(payload.get("project") or args.get("project") or args.get("project_id") or "mnemoforge").strip() or "mnemoforge"
    context_type = _semantic_rules_context_type(facade=facade, route=route, args=args)
    intent_text = " ".join(
        [
            str(args.get("intent") or ""),
            str(args.get("task") or ""),
            str(route.get("reason") or ""),
            str(route.get("intent_type") or ""),
            str(route.get("tool") or ""),
        ]
    ).strip()
    intent_tokens = _semantic_tokens(intent_text)

    try:
        candidates = get_active_operational_instincts(
            context_type=context_type,
            project_id=project,
            limit=25,
            record_activation=False,
        )
    except Exception as exc:
        return {
            "status": "fallback",
            "fallback_used": True,
            "reason": f"rules_unavailable:{format_error(exc)}",
            "context_type": context_type,
            "project": project,
            "matched_rules": [],
            "applied_rule_count": 0,
            "blocked": False,
            "preconditions": [],
            "block_error": "",
        }

    scored: list[tuple[float, dict[str, Any]]] = []
    rank_bonus = {"P0": 2.0, "P1": 1.0, "P2": 0.4}
    now = time.time()
    for item in candidates:
        trigger = str(item.get("trigger") or "")
        action = str(item.get("action") or "")
        hay_tokens = _semantic_tokens(f"{trigger} {action} {' '.join(item.get('activation_tags') or [])}")
        overlap = len(intent_tokens.intersection(hay_tokens))
        score = float(overlap) + rank_bonus.get(str(item.get("rank") or ""), 0.0)
        if score <= 0:
            continue
        updated_at = float(item.get("updated_at") or 0.0)
        if updated_at > 0:
            age_days = max(0.0, (now - updated_at) / 86400.0)
            score += max(0.0, 0.5 - min(0.5, age_days / 30.0))
        scored.append((score, item))
    scored.sort(key=lambda row: row[0], reverse=True)
    top = [item for _, item in scored[:5]]

    command_text = " ".join(
        [
            str(args.get("command") or ""),
            str(args.get("test_command") or ""),
            str(args.get("cmd") or ""),
            " ".join(str(v) for v in (args.get("verification") or [])),
            str(args.get("intent") or ""),
        ]
    ).lower()
    verification_policy = _semantic_verification_spec()
    trigger_terms = _semantic_string_list(verification_policy.get("trigger_terms"))
    approved_terms = _semantic_string_list(verification_policy.get("approved_contour_terms"))
    docker_rule_terms = _semantic_string_list(verification_policy.get("docker_rule_terms"))
    docker_rule_required_terms = _semantic_string_list(verification_policy.get("docker_rule_required_terms"))
    host_command_terms = _semantic_string_list(verification_policy.get("host_command_terms"))
    wants_test = any(token in command_text for token in trigger_terms)
    docker_rule_active = any(
        (
            bool(docker_rule_required_terms)
            and all(term in str(item.get("action") or "").lower() for term in docker_rule_required_terms)
        )
        or any(term in str(item.get("action") or "").lower() for term in docker_rule_terms)
        for item in top
    )
    if wants_test and not docker_rule_active and facade in {"project_verify", "project_work"}:
        docker_rule_active = True
    blocked = bool(
        wants_test
        and docker_rule_active
        and any(term in command_text for term in host_command_terms)
        and not any(term in command_text for term in approved_terms)
    )

    preconditions: list[dict[str, Any]] = []
    if wants_test and docker_rule_active:
        preconditions.append(
            {
                "id": str(verification_policy.get("id") or "").strip(),
                "required": True,
                "satisfied": not blocked,
                "message": str(verification_policy.get("message") or "").strip(),
            }
        )

    task_id = str(payload.get("task_id") or args.get("task_id") or "").strip()
    verification_recovery = None
    if blocked:
        verification_recovery = _semantic_recovery_packet(
            policy=verification_policy,
            project=project,
            task_id=task_id,
            changed_files=args.get("changed_files") or [],
        )

    return {
        "status": "applied" if top else "fallback",
        "fallback_used": not bool(top),
        "reason": "no_semantic_rule_match" if not top else "",
        "context_type": context_type,
        "project": project,
        "intent": intent_text[:240],
        "matched_rules": [
            {
                "instinct_id": str(item.get("instinct_id") or ""),
                "rank": str(item.get("rank") or ""),
                "action": str(item.get("action") or "")[:280],
                "activation_tags": list(item.get("activation_tags") or []),
            }
            for item in top
        ],
        "applied_rule_count": len(top),
        "blocked": blocked,
        "preconditions": preconditions,
        "block_error": str(verification_policy.get("block_error") or "").strip() if blocked else "",
        "verification_recovery": verification_recovery,
        "recommended_next_call": verification_recovery.get("recommended_next_call") if verification_recovery else None,
        "next_safe_action": verification_recovery.get("next_safe_action") if verification_recovery else "",
    }
