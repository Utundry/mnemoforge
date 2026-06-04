from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec


@lru_cache(maxsize=1)
def _incident_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("diagnostics/public_incidents.json")
    except Exception:
        return {"incidents": {}}


def _incident_template(kind: str) -> dict[str, Any]:
    incidents = _incident_spec().get("incidents")
    if not isinstance(incidents, dict):
        return {}
    template = incidents.get(kind)
    return dict(template) if isinstance(template, dict) else {}


def build_public_diagnostic_incident(
    *,
    kind: str,
    safe_next_action: str = "",
    severity: str = "",
    summary: str = "",
    why: str = "",
    likely_source: str = "",
    resource_kind: str = "",
    missing_fields: list[str] | None = None,
    task_id: str = "",
    recommended_next_call: dict[str, Any] | None = None,
    expand_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Build a compact public diagnostic packet without internal telemetry noise."""
    template = _incident_template(kind)
    packet: dict[str, Any] = {
        "kind": kind,
        "severity": severity or str(template.get("severity") or ""),
        "summary": summary or str(template.get("summary") or ""),
        "why": why or str(template.get("why") or ""),
        "likely_source": likely_source or str(template.get("likely_source") or ""),
        "resource_kind": resource_kind or str(template.get("resource_kind") or ""),
        "missing_fields": missing_fields if missing_fields is not None else list(template.get("missing_fields") or []),
        "task_id": task_id,
        "safe_next_action": safe_next_action,
        "recommended_next_call": recommended_next_call or {},
        "expand_refs": expand_refs if expand_refs is not None else list(template.get("expand_refs") or []),
    }
    return {key: value for key, value in packet.items() if value not in (None, "", [], {})}


def attach_public_diagnostic_incident(
    *,
    receipt: dict[str, Any],
    kind: str,
    safe_next_action: str = "",
    resource_kind: str = "",
    missing_fields: list[str] | None = None,
    task_id: str = "",
    recommended_next_call: dict[str, Any] | None = None,
    expand_refs: list[str] | None = None,
) -> dict[str, Any]:
    updated = dict(receipt)
    updated["diagnostic_incident"] = build_public_diagnostic_incident(
        kind=kind,
        safe_next_action=safe_next_action or str(updated.get("next_safe_action") or ""),
        resource_kind=resource_kind or str(updated.get("resource_kind") or ""),
        missing_fields=missing_fields if missing_fields is not None else list(updated.get("missing_fields") or []),
        task_id=task_id,
        recommended_next_call=recommended_next_call,
        expand_refs=expand_refs,
    )
    return updated
