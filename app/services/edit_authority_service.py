from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any, Iterable

from app.services.mcp_workflow_specs import load_named_json_spec
from app.services.public_diagnostic_service import build_public_diagnostic_incident


@lru_cache(maxsize=1)
def _edit_authority_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/edit_authority.json")
    except Exception:
        return {
            "severity": "P0",
            "approval_intent": "user_approved_start",
            "state_defaults": {},
            "states": {},
            "scope_drift": {"ambiguity_is_drift": True, "dimensions": []},
        }


def build_edit_authority(
    *,
    state: str,
    task_id: str = "",
    approved_framing: str = "",
    framing_version: str = "",
    approval_intent: str = "",
    drift_dimensions: Iterable[str] | None = None,
    ambiguous: bool = False,
    autonomous_mode: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = _edit_authority_spec()
    drift_spec = spec.get("scope_drift") if isinstance(spec.get("scope_drift"), dict) else {}
    known_dimensions = [str(item) for item in drift_spec.get("dimensions") or [] if str(item)]
    reported_drift = sorted({str(item).strip() for item in drift_dimensions or [] if str(item).strip()})
    unknown_drift = sorted(set(reported_drift) - set(known_dimensions))
    has_drift = bool(reported_drift) or (ambiguous and bool(drift_spec.get("ambiguity_is_drift", True)))
    expected_approval = str(spec.get("approval_intent") or "user_approved_start")
    autonomous_approved = bool(
        isinstance(autonomous_mode, dict)
        and autonomous_mode.get("authority_granted")
        and str(autonomous_mode.get("mode") or "") == "explicit_autonomous_mode"
    )
    approved = (
        str(state or "").strip() == "implementation"
        and (
            (
                str(approval_intent or "").strip() == expected_approval
                and bool(str(approved_framing or "").strip())
            )
            or autonomous_approved
        )
    )

    if has_drift:
        authority = "scope_drift_stop"
    elif approved:
        authority = "approved_implementation"
    else:
        defaults = spec.get("state_defaults") if isinstance(spec.get("state_defaults"), dict) else {}
        authority = str(defaults.get(str(state or "").strip()) or "no_authority")

    states = spec.get("states") if isinstance(spec.get("states"), dict) else {}
    state_spec = states.get(authority) if isinstance(states.get(authority), dict) else {}
    framing_text = str(approved_framing or "").strip()
    version = str(framing_version or "").strip() or _framing_version(task_id=task_id, framing=framing_text)
    packet = {
        "status": authority,
        "severity": str(spec.get("severity") or "P0"),
        "tool_independent": True,
        "advisory_enforcement": True,
        "editing_allowed": bool(state_spec.get("editing_allowed", False)),
        "ability_vs_authority": "Technical ability to edit does not grant authority to edit.",
        "instruction": str(state_spec.get("instruction") or ""),
        "task_id": str(task_id or "").strip(),
        "framing_version": version if (framing_text or autonomous_approved) else "",
        "approval_intent": expected_approval,
        "authority_source": (
            "explicit_autonomous_mode"
            if autonomous_approved
            else "explicit_user_approval"
            if approved
            else ""
        ),
        "approval_applies_only_to_latest_framing": True,
        "generic_continuation_is_not_approval": list(spec.get("generic_continuation_terms") or []),
        "scope_drift": {
            "ambiguity_is_drift": bool(drift_spec.get("ambiguity_is_drift", True)),
            "dimensions": known_dimensions,
            "reported_dimensions": reported_drift,
            "unknown_dimensions": unknown_drift,
            "not_drift": str(drift_spec.get("not_drift") or ""),
        },
        "next_safe_action": str(state_spec.get("next_safe_action") or ""),
    }
    if framing_text:
        packet["approved_framing"] = framing_text
    if autonomous_approved:
        packet["autonomous_mode"] = autonomous_mode
    if authority == "scope_drift_stop":
        packet["adherence_incident"] = build_public_diagnostic_incident(
            kind="edit_authority_scope_drift",
            task_id=str(task_id or "").strip(),
            safe_next_action=packet["next_safe_action"],
        )
    return _compact(packet)


def _framing_version(*, task_id: str, framing: str) -> str:
    if not framing:
        return ""
    canonical = json.dumps(
        {"task_id": str(task_id or "").strip(), "framing": " ".join(framing.split())},
        ensure_ascii=True,
        sort_keys=True,
    )
    return f"framing:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}
