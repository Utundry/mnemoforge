from __future__ import annotations

import re
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec


def _learning_eligibility_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("learning/eligibility.json")
    except Exception:
        return {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "allow", "allowed", "approved"}


def _contains_marker(text: str, markers: list[str]) -> str:
    lowered = str(text or "").casefold()
    for marker in markers:
        clean = str(marker or "").strip().casefold()
        if not clean:
            continue
        if re.fullmatch(r"[\w_:-]+", clean, flags=re.UNICODE):
            if re.search(rf"\b{re.escape(clean)}\b", lowered, flags=re.UNICODE):
                return clean
        elif clean in lowered:
            return clean
    return ""


def evaluate_learning_eligibility(
    *,
    source: str = "",
    metadata: dict[str, Any] | None = None,
    pattern: str = "",
) -> dict[str, Any]:
    spec = _learning_eligibility_spec()
    meta = metadata if isinstance(metadata, dict) else {}
    source_name = str(source or "").strip().casefold()
    approved_sources = {str(item or "").strip().casefold() for item in spec.get("approved_sources") or []}
    blocked_sources = {str(item or "").strip().casefold() for item in spec.get("blocked_sources") or []}
    blocked_event_classes = {str(item or "").strip().casefold() for item in spec.get("blocked_event_classes") or []}
    blocked_states = {str(item or "").strip().casefold() for item in spec.get("blocked_states") or []}
    explicit_allow_keys = [str(item or "").strip() for item in spec.get("explicit_allow_keys") or []]
    explicit_block_keys = [str(item or "").strip() for item in spec.get("explicit_block_keys") or []]
    markers = [str(item or "").strip().casefold() for item in spec.get("diagnostic_markers") or []]

    if any(key and _truthy(meta.get(key)) for key in explicit_allow_keys):
        return _decision(True, "explicit_allow", "Learning was explicitly approved.", meta)

    for key in explicit_block_keys:
        if key and _truthy(meta.get(key)):
            return _decision(False, "explicit_block", f"Learning blocked by metadata flag: {key}.", meta)

    if source_name in approved_sources:
        return _decision(True, "approved_source", f"Learning allowed for approved source: {source_name}.", meta)

    if source_name in blocked_sources or any(source_name.startswith(f"{item}:") for item in blocked_sources):
        return _decision(False, "blocked_source", f"Learning blocked for source: {source_name}.", meta)

    event_class = str(
        meta.get("source_event_class")
        or meta.get("event_class")
        or meta.get("learning_event_class")
        or ""
    ).strip().casefold()
    if event_class in blocked_event_classes:
        return _decision(False, "blocked_event_class", f"Learning blocked for event class: {event_class}.", meta)

    state = str(meta.get("state") or meta.get("workflow_state") or "").strip().casefold()
    if state in blocked_states:
        return _decision(False, "blocked_state", f"Learning blocked for workflow state: {state}.", meta)

    if _truthy(meta.get("diagnostic")) or str(meta.get("response_format") or "").strip().casefold() == "diagnostic":
        return _decision(False, "diagnostic", "Learning blocked for diagnostic request context.", meta)

    marker = _contains_marker(" ".join([str(pattern or ""), str(meta)]), markers)
    if marker:
        return _decision(False, "diagnostic_marker", f"Learning blocked by diagnostic marker: {marker}.", meta)

    return _decision(
        str(spec.get("default_decision") or "allow").strip().casefold() != "block",
        "default",
        "Learning allowed by default eligibility policy.",
        meta,
    )


def _decision(eligible: bool, decision: str, reason: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "eligible": bool(eligible),
        "decision": decision,
        "reason": reason,
        "metadata": {
            **metadata,
            "learning_eligibility": {
                "eligible": bool(eligible),
                "decision": decision,
                "reason": reason,
            },
        },
    }
