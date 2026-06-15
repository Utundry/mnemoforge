from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec


def build_refinement_lifecycle(
    *,
    project: str,
    payload: dict[str, Any],
    target_ref: str,
    target_type: str,
    action: str,
    actor: str,
    adapter: str,
    default_observed: str = "",
    default_expected: str = "",
) -> dict[str, Any]:
    spec = load_named_json_spec("governance/refinement_lifecycle.json")
    evidence_refs = _string_list(payload.get("evidence_refs"))
    reason = str(payload.get("reason") or "").strip()
    observed = str(payload.get("observed_behavior") or default_observed or reason).strip()
    expected = str(payload.get("expected_behavior") or default_expected).strip()
    proposed = str(payload.get("proposed_refinement") or "").strip()
    authority_mode = str(payload.get("authority") or "").strip()
    if not authority_mode:
        authority_mode = "explicit_operator_intent" if bool(payload.get("apply", False)) else "preview_only"
    phases = [
        {
            "id": phase,
            "status": "complete" if phase in {"observe", "identify", "diagnose", "propose"} else "pending",
        }
        for phase in spec.get("phases") or []
    ]
    return {
        "contract": str(spec.get("id") or "governed_refinement_lifecycle"),
        "version": int(spec.get("version") or 1),
        "target": {"project": project, "ref": target_ref, "type": target_type},
        "observation": {
            "observed_behavior": observed,
            "expected_behavior": expected,
            "provenance": str(payload.get("provenance") or payload.get("source") or "operator_feedback").strip(),
            "evidence_refs": evidence_refs,
            "confidence": _confidence(payload.get("confidence")),
        },
        "diagnosis": {"reason": reason},
        "proposal": {"action": action, "description": proposed or reason},
        "authority": {
            "mode": authority_mode,
            "actor": actor,
            "apply_requested": bool(payload.get("apply", False)),
        },
        "adapter": {"id": adapter, "kind": "governed_runtime_adapter"},
        "postcondition": {"status": "pending", "expected": {}, "actual": {}, "satisfied": None},
        "audit": {
            "status": "pending",
            "evidence_refs": evidence_refs,
            "mutation_executed": False,
            "reversible": False,
            "reversal_action": "",
        },
        "phases": phases,
    }


def complete_refinement_lifecycle(
    lifecycle: dict[str, Any],
    *,
    status: str,
    mutation_executed: bool,
    postcondition_expected: dict[str, Any] | None = None,
    postcondition_actual: dict[str, Any] | None = None,
    postcondition_satisfied: bool | None = None,
    audit_evidence: list[str] | None = None,
    reversible: bool = False,
    reversal_action: str = "",
    adapter_kind: str = "",
) -> dict[str, Any]:
    completed = deepcopy(lifecycle)
    if adapter_kind:
        completed["adapter"]["kind"] = adapter_kind
    completed["postcondition"] = {
        "status": "verified" if postcondition_satisfied is not None else "not_evaluated",
        "expected": postcondition_expected or {},
        "actual": postcondition_actual or {},
        "satisfied": postcondition_satisfied,
    }
    existing_evidence = _string_list(completed.get("audit", {}).get("evidence_refs"))
    completed["audit"] = {
        "status": "recorded",
        "evidence_refs": _dedupe([*existing_evidence, *_string_list(audit_evidence)]),
        "mutation_executed": bool(mutation_executed),
        "reversible": bool(reversible),
        "reversal_action": str(reversal_action or "").strip(),
    }
    completed["outcome_status"] = status
    phase_status = {
        "review_apply": "complete" if status != "needs_input" else "blocked",
        "verify": "complete" if postcondition_satisfied is not None else "not_applicable",
        "audit": "complete",
    }
    for phase in completed.get("phases") or []:
        phase_id = str(phase.get("id") or "")
        if phase_id in phase_status:
            phase["status"] = phase_status[phase_id]
    return completed


def _confidence(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(max(0.0, min(float(value), 1.0)), 3)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
