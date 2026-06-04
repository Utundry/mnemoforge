from __future__ import annotations

from typing import Any

from app.services.data_hygiene_service import (
    build_maintenance_suggestion,
    build_operator_playbook,
    get_data_hygiene_store,
)
from app.services.data_integrity_service import get_data_integrity_store


def build_storage_trust_report(*, limit: int = 1000, current_project: str | None = None) -> dict[str, Any]:
    integrity = get_data_integrity_store().overview()
    hygiene = get_data_hygiene_store().overview(current_project=current_project)
    playbook = build_operator_playbook(limit=limit, current_project=current_project)

    integrity_status = integrity.get("status", "ok")
    hygiene_status = hygiene.get("status", "ok")
    if integrity_status == "degraded":
        overall_status = "degraded"
    elif hygiene_status == "warning":
        overall_status = "warning"
    else:
        overall_status = "ok"

    next_actions: list[str] = []
    if integrity.get("degraded_slices"):
        next_actions.append(
            "Investigate degraded integrity slices before trusting affected storage filters and retrieval paths."
        )
        for slice_id, remediations in (integrity.get("recommended_remediations") or {}).items():
            if remediations:
                next_actions.append(
                    f"Integrity remediation available for {slice_id}: {remediations[0].get('action_type')}"
                )
    workflow = playbook.get("workflow", {})
    manual_review_pending = workflow.get("manual_review_pending", {})
    quarantine_candidates = workflow.get("quarantine_candidates", {})
    delete_ready = workflow.get("delete_ready", {})
    if manual_review_pending:
        next_actions.append(
            "Review manual-review hygiene findings before running any destructive cleanup."
        )
    scope_warnings = ((playbook.get("workflow") or {}).get("scope_summary") or {}).get("warnings") or []
    if scope_warnings:
        next_actions.append(
            "Review hygiene scope warnings before presenting maintenance as current-project work."
        )
    if quarantine_candidates:
        next_actions.append(
            "Use reviewed-delete preview before executing reviewed deletes for quarantined synthetic/test traces."
        )
    if delete_ready:
        next_actions.append(
            "Run delete-dry-run before approved delete for live qdrant memories."
        )

    if overall_status == "ok":
        summary = "Storage trust is healthy: no degraded integrity slices and no active hygiene warnings."
    elif overall_status == "warning":
        summary = "Storage trust is warning: storage is reachable, but hygiene issues still require operator review."
    else:
        summary = "Storage trust is degraded: at least one integrity slice is unhealthy and operator action is required."

    return {
        "status": overall_status,
        "summary": summary,
        "integrity": integrity,
        "data_hygiene": hygiene,
        "maintenance_suggestion": build_maintenance_suggestion(limit=limit, current_project=current_project),
        "playbook": playbook,
        "next_actions": next_actions,
        "signals": {
            "degraded_slices": integrity.get("degraded_slices", []),
            "active_hygiene_findings": hygiene.get("active_findings", 0),
            "manual_review_pending": manual_review_pending,
            "quarantine_candidates": quarantine_candidates,
            "delete_ready": delete_ready,
            "hygiene_scope_warnings": scope_warnings,
        },
    }
