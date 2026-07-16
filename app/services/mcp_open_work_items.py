"""Open work item preparation and annotation helpers for MCP facades."""
from __future__ import annotations

from typing import Any, Callable

from app.services.mcp_artifact_refs import artifact_type_from_key
from app.services.task_reconciliation_service import COVERED_DECISIONS, get_task_reconciliation_store

ErrorFormatter = Callable[[Exception], str]


def task_id_from_open_task_item(item: dict[str, Any]) -> str:
    task_id = str(item.get("task_id") or item.get("id") or "").strip()
    if task_id:
        return task_id
    artifact_key = str(item.get("artifact_key") or "").strip()
    if artifact_key.startswith("task:"):
        parts = artifact_key.split(":")
        if len(parts) >= 3:
            return parts[-1].strip()
    return ""


def open_work_priority(item: dict[str, Any]) -> float:
    explicit = item.get("importance_score")
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    tags = {str(tag).strip().casefold() for tag in (item.get("tags") or []) if str(tag).strip()}
    if tags & {"priority:critical", "critical"}:
        return 1.0
    if tags & {"priority:high", "high_priority"}:
        return 0.9
    if tags & {"priority:low", "low_priority"}:
        return 0.4
    return 0.7


def reconciliation_refs_for_open_item(item: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("artifact_key", "linked_artifact_key"):
        value = str(item.get(key) or "").strip()
        if value and value not in refs:
            refs.append(value)
    project = str(item.get("project") or "").strip()
    item_id = str(item.get("task_id") or item.get("id") or "").strip()
    if project and item_id:
        for prefix in ("task", "improvement"):
            ref = f"{prefix}:{project}:{item_id}"
            if ref not in refs:
                refs.append(ref)
    return refs


def covered_reconciliation_packet_for_open_item(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        store = get_task_reconciliation_store()
    except Exception:
        return None
    for ref in reconciliation_refs_for_open_item(item):
        packet = store.packet_for_target(ref)
        decision = str(packet.get("decision") or "").strip().lower()
        if packet.get("status") == "reviewed" and decision in COVERED_DECISIONS:
            return packet
    return None


def prepare_open_work_items(data: dict[str, Any], *, limit: int) -> dict[str, Any]:
    items = data.get("items") or []
    if not isinstance(items, list):
        return data

    terminal_statuses = {"done", "resolved", "completed", "closed", "cancelled", "archived"}
    improvement_keys = {
        str(item.get("artifact_key") or "").strip()
        for item in items
        if isinstance(item, dict) and str(item.get("type") or "").strip() == "improvement"
    }
    visible: list[dict[str, Any]] = []
    reconciled: list[dict[str, Any]] = []
    suppressed_projection_count = 0
    suppressed_completed_count = 0
    suppressed_reconciled_count = 0
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        item_type = str(item.get("type") or "").strip() or artifact_type_from_key(item.get("artifact_key"))
        item_status = str(item.get("status") or "").strip().lower()
        linked_status = str(item.get("linked_status") or "").strip().lower()
        if item_status in terminal_statuses or linked_status in terminal_statuses:
            suppressed_completed_count += 1
            continue
        is_projection = (
            item_type == "task"
            and str(item.get("source") or "").strip() == "improvement"
            and str(item.get("linked_artifact_key") or "").strip() in improvement_keys
        )
        if is_projection:
            suppressed_projection_count += 1
            continue
        reconciliation_packet = covered_reconciliation_packet_for_open_item(item)
        if reconciliation_packet:
            suppressed_reconciled_count += 1
            reconciled.append(
                {
                    "artifact_key": item.get("artifact_key"),
                    "linked_artifact_key": item.get("linked_artifact_key"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "reconciliation": reconciliation_packet,
                    "next_safe_action": reconciliation_packet.get("next_safe_action"),
                }
            )
            continue
        item["work_priority"] = open_work_priority(item)
        if item_type == "improvement":
            item["lifecycle_stage"] = str(item.get("stage") or "proposal")
            item["implementation_ready"] = False
            item["claim_allowed"] = False
            item["framing_required"] = True
        visible.append(item)

    visible.sort(
        key=lambda item: (
            float(item.get("work_priority") or 0.0),
            str(item.get("updated_at") or item.get("created_at") or ""),
        ),
        reverse=True,
    )
    visible = visible[: max(1, int(limit))]
    enriched = dict(data)
    enriched["items"] = visible
    enriched["total"] = len(visible)
    enriched["priority_policy"] = "Unified cross-type priority with lifecycle-specific next actions."
    if suppressed_projection_count:
        enriched["suppressed_projection_count"] = suppressed_projection_count
    if suppressed_completed_count:
        enriched["suppressed_completed_count"] = suppressed_completed_count
    if suppressed_reconciled_count:
        enriched["suppressed_reconciled_count"] = suppressed_reconciled_count
        enriched["reconciliation_warning"] = (
            "Some open work items were hidden from ordinary next-priority results because an "
            "operator-reviewed reconciliation decision marks them covered by implemented work."
        )
        enriched["reconciled_items"] = reconciled[:10]
    return enriched


def annotate_open_tasks_with_claims(data: dict[str, Any], args: dict[str, Any], *, format_error: ErrorFormatter) -> dict[str, Any]:
    claim_filter = str(args.get("claim_filter") or "available").strip().lower()
    if claim_filter not in {"available", "claimed", "all"}:
        claim_filter = "available"
    include_claims = bool(args.get("include_claims", True))
    if not include_claims and claim_filter == "all":
        return data

    items = data.get("items") or []
    if not isinstance(items, list):
        return data
    if not items:
        enriched = dict(data)
        enriched["claim_filter"] = claim_filter
        enriched["claim_summary"] = {"available": 0, "claimed": 0, "returned": 0, "hidden_claimed": 0}
        return enriched

    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    try:
        from app.services.task_lease_service import get_task_lease_store

        store = get_task_lease_store()
    except Exception as exc:
        enriched = dict(data)
        enriched["claim_filter"] = claim_filter
        enriched["claim_summary"] = {
            "available": len([item for item in items if isinstance(item, dict)]),
            "claimed": 0,
            "returned": len([item for item in items if isinstance(item, dict)]),
            "hidden_claimed": 0,
            "unavailable": True,
        }
        enriched.setdefault("warnings", [])
        if isinstance(enriched["warnings"], list):
            enriched["warnings"].append(f"Task claim annotations unavailable: {format_error(exc)}")
        return enriched
    visible: list[dict[str, Any]] = []
    hidden_claimed_count = 0
    claimed_count = 0
    available_count = 0

    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        task_id = task_id_from_open_task_item(item)
        lease = store.get_active_claim(project=project, task_id=task_id) if task_id else None
        if lease:
            claimed_count += 1
            item["claim_status"] = "claimed"
            item["claim_available"] = False
            item["task_claim"] = lease.model_dump(mode="json")
        else:
            available_count += 1
            if include_claims:
                item["claim_status"] = "available"
                item["claim_available"] = True
                item["task_claim"] = None

        if claim_filter == "available" and lease:
            hidden_claimed_count += 1
            continue
        if claim_filter == "claimed" and not lease:
            continue
        visible.append(item)

    enriched = dict(data)
    enriched["items"] = visible
    enriched["claim_filter"] = claim_filter
    enriched["claim_summary"] = {
        "available": available_count,
        "claimed": claimed_count,
        "returned": len(visible),
        "hidden_claimed": hidden_claimed_count,
    }
    if hidden_claimed_count:
        enriched["hidden_claimed_count"] = hidden_claimed_count
    return enriched


def task_assignment_safety(item: dict[str, Any]) -> dict[str, Any]:
    tags = {str(tag).strip().casefold() for tag in (item.get("tags") or []) if str(tag).strip()}
    if item.get("claim_status") == "claimed":
        return {
            "state": "blocked",
            "assignable": False,
            "reason": "task_is_already_claimed",
            "requires_review": False,
        }
    if bool(item.get("task_statement_incomplete")):
        return {
            "state": "needs_review",
            "assignable": False,
            "reason": "task_statement_incomplete",
            "requires_review": True,
        }
    dependency_fields = ("depends_on", "blocked_by", "sequential_after", "related_task_ids")
    if any(item.get(field) for field in dependency_fields) or tags & {"dependent", "blocked", "sequential", "needs_dependency_review"}:
        return {
            "state": "needs_review",
            "assignable": False,
            "reason": "dependency_or_sequence_marker_present",
            "requires_review": True,
        }
    if item.get("parallel_safe") is True or item.get("assignment_safety") == "independent" or tags & {"parallel_safe", "independent", "multi_agent_safe"}:
        return {
            "state": "independent",
            "assignable": True,
            "reason": "explicit_independent_marker",
            "requires_review": False,
        }
    return {
        "state": "needs_review",
        "assignable": False,
        "reason": "no_explicit_independence_evidence",
        "requires_review": True,
    }


def annotate_open_tasks_with_assignment_safety(data: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    assignment_filter = str(args.get("assignment_filter") or "all").strip().lower()
    if assignment_filter not in {"all", "independent", "needs_review"}:
        assignment_filter = "all"
    items = data.get("items") or []
    if not isinstance(items, list):
        return data

    visible: list[dict[str, Any]] = []
    summary = {"independent": 0, "needs_review": 0, "blocked": 0, "returned": 0, "hidden": 0}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        safety = task_assignment_safety(item)
        item["assignment_safety"] = safety
        state = str(safety["state"])
        if state in summary:
            summary[state] += 1
        if assignment_filter == "independent" and state != "independent":
            summary["hidden"] += 1
            continue
        if assignment_filter == "needs_review" and state != "needs_review":
            summary["hidden"] += 1
            continue
        visible.append(item)

    summary["returned"] = len(visible)
    enriched = dict(data)
    enriched["items"] = visible
    enriched["assignment_filter"] = assignment_filter
    enriched["assignment_summary"] = summary
    if assignment_filter == "independent":
        enriched["assignment_policy"] = (
            "Only tasks with explicit independence evidence are returned for multi-agent assignment; "
            "unclaimed alone is not enough."
        )
    return enriched
