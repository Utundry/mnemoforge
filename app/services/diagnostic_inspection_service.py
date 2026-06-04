from __future__ import annotations

from typing import Any

from app.services.learning_eligibility_service import evaluate_learning_eligibility
from app.services.mcp_workflow_specs import load_route_catalog_spec
from app.services.public_ref_index import get_public_ref_index_store, is_short_public_id
from app.services.route_pattern_store import get_route_pattern_store


def build_diagnostic_inspection_packet(
    *,
    project: str,
    payload: dict[str, Any],
    diagnostic: bool = False,
) -> dict[str, Any]:
    target = str(payload.get("target") or "all").strip().casefold() or "all"
    limit = max(1, min(int(payload.get("limit") or 20), 100))
    sections: dict[str, Any] = {}

    if target in {"all", "route", "route_pattern", "routing"}:
        sections["routing"] = _routing_section(payload=payload, limit=limit, diagnostic=diagnostic)
    if target in {"all", "learning", "eligibility"}:
        sections["learning"] = _learning_section(payload=payload)
    if target in {"all", "artifact", "ref", "public_ref"}:
        sections["artifact_ref"] = _artifact_ref_section(project=project, payload=payload, limit=limit)

    findings = _collect_findings(sections)
    return {
        "status": "ok",
        "target": target,
        "read_only": True,
        "summary": {
            "sections": sorted(sections.keys()),
            "findings": len(findings),
            "likely_source": _likely_source(findings),
        },
        "findings": findings[:limit],
        "sections": sections,
        "next_diagnostic_action": _next_diagnostic_action(findings),
    }


def _routing_section(*, payload: dict[str, Any], limit: int, diagnostic: bool) -> dict[str, Any]:
    facade = str(payload.get("facade") or "").strip()
    query = str(payload.get("query") or "").strip()
    store = get_route_pattern_store()
    hygiene = store.hygiene_report(
        known_tools=_known_route_tools(),
        limit=limit,
        stale_after_days=max(1, min(int(payload.get("stale_after_days") or 30), 365)),
    )
    if facade:
        hygiene = _filter_route_hygiene(hygiene, facade=facade)

    matched_route = None
    if facade and query:
        matched_route = store.preview_match(facade=facade, pattern=query)

    section: dict[str, Any] = {
        "status": hygiene.get("status", "ok"),
        "summary": hygiene.get("summary", {}),
        "findings": hygiene.get("findings", [])[:limit],
    }
    if matched_route:
        section["matched_route"] = _compact_route(matched_route, diagnostic=diagnostic)
    if diagnostic:
        section["patterns"] = [_compact_route(item, diagnostic=True) for item in hygiene.get("patterns", [])[:limit]]
        section["disabled_patterns"] = [
            _compact_route(item, diagnostic=True)
            for item in hygiene.get("disabled_patterns", [])[: min(limit, 25)]
        ]
    return section


def _learning_section(*, payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    source = str(payload.get("source") or metadata.get("source") or "").strip()
    pattern = str(payload.get("query") or payload.get("pattern") or "").strip()
    eligibility = evaluate_learning_eligibility(source=source, metadata=metadata, pattern=pattern)
    return {
        "status": "ok",
        "eligible": bool(eligibility.get("eligible")),
        "decision": eligibility.get("decision"),
        "reason": eligibility.get("reason"),
    }


def _artifact_ref_section(*, project: str, payload: dict[str, Any], limit: int) -> dict[str, Any]:
    ref = str(payload.get("ref") or payload.get("query") or "").strip()
    requested_type = str(payload.get("artifact_type") or payload.get("type") or "all").strip() or "all"
    short_id = _short_id_from_ref(ref)
    if not short_id:
        return {
            "status": "needs_ref",
            "message": "Provide a short public id or artifact ref to inspect public-ref resolution.",
        }

    matches = get_public_ref_index_store().find(
        project=project,
        requested_type=requested_type,
        short_id=short_id,
    )[:limit]
    status = "not_found" if not matches else "ambiguous" if len(matches) > 1 else "resolved"
    return {
        "status": status,
        "requested_type": requested_type,
        "short_id": short_id,
        "matches": [_compact_ref_match(item) for item in matches],
    }


def _short_id_from_ref(ref: str) -> str:
    text = str(ref or "").strip()
    if is_short_public_id(text):
        return text
    parts = text.split(":")
    candidate = parts[-1] if parts else ""
    return candidate if is_short_public_id(candidate) else ""


def _filter_route_hygiene(hygiene: dict[str, Any], *, facade: str) -> dict[str, Any]:
    filtered = dict(hygiene)
    for key in ("patterns", "disabled_patterns", "findings"):
        items = filtered.get(key)
        if isinstance(items, list):
            filtered[key] = [item for item in items if str(item.get("facade") or "") == facade]
    summary = filtered.get("summary") if isinstance(filtered.get("summary"), dict) else {}
    filtered["summary"] = {
        **summary,
        "facade_filter": facade,
        "returned_patterns": len(filtered.get("patterns") or []),
        "returned_disabled_patterns": len(filtered.get("disabled_patterns") or []),
        "returned_findings": len(filtered.get("findings") or []),
    }
    return filtered


def _compact_route(route: dict[str, Any], *, diagnostic: bool) -> dict[str, Any]:
    compact = {
        "pattern_id": route.get("pattern_id"),
        "facade": route.get("facade"),
        "intent_type": route.get("intent_type"),
        "tool": route.get("tool"),
        "mutating": bool(route.get("mutating")),
        "confidence": route.get("confidence"),
        "source": route.get("source"),
        "matched_by": route.get("matched_by"),
    }
    if diagnostic:
        compact["metadata"] = route.get("metadata") if isinstance(route.get("metadata"), dict) else {}
        compact["hit_count"] = route.get("hit_count")
        compact["feedback"] = {
            "positive": route.get("positive_feedback"),
            "negative": route.get("negative_feedback"),
        }
    return {key: value for key, value in compact.items() if value not in (None, "", {})}


def _compact_ref_match(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_key": item.get("artifact_key"),
        "ref_kind": item.get("ref_kind"),
        "local_id": item.get("local_id"),
        "linked_artifact_key": item.get("linked_artifact_key"),
        "title": item.get("title"),
        "status": item.get("status"),
    }


def _collect_findings(sections: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    routing = sections.get("routing")
    if isinstance(routing, dict):
        for item in routing.get("findings") or []:
            if isinstance(item, dict):
                findings.append({"section": "routing", **item})

    learning = sections.get("learning")
    if isinstance(learning, dict) and not learning.get("eligible", True):
        findings.append(
            {
                "section": "learning",
                "type": "learning_blocked",
                "severity": "info",
                "decision": learning.get("decision"),
                "reason": learning.get("reason"),
            }
        )

    artifact_ref = sections.get("artifact_ref")
    if isinstance(artifact_ref, dict) and artifact_ref.get("status") in {"ambiguous", "not_found"}:
        findings.append(
            {
                "section": "artifact_ref",
                "type": f"public_ref_{artifact_ref.get('status')}",
                "severity": "medium" if artifact_ref.get("status") == "ambiguous" else "low",
                "reason": "Public ref resolution did not produce exactly one match.",
            }
        )
    return findings


def _likely_source(findings: list[dict[str, Any]]) -> str:
    if any(item.get("section") == "routing" and item.get("severity") == "high" for item in findings):
        return "route_pattern_store"
    if any(item.get("section") == "artifact_ref" for item in findings):
        return "public_ref_index"
    if any(item.get("section") == "learning" for item in findings):
        return "learning_eligibility_policy"
    return "none"


def _next_diagnostic_action(findings: list[dict[str, Any]]) -> str:
    if any(item.get("section") == "routing" for item in findings):
        return "Use route_feedback only after confirming a concrete misroute."
    if any(item.get("section") == "artifact_ref" for item in findings):
        return "Refine the artifact type or use a longer public id prefix."
    if any(item.get("section") == "learning" for item in findings):
        return "Keep diagnostic events out of learned aliases unless an operator explicitly approves training."
    return "Continue normal workflow; no diagnostic issue was found in the inspected contour."


def _known_route_tools() -> set[str]:
    tools: set[str] = set()
    for facade in ("project_work", "project_rules", "project_context", "project_verify", "project_capture"):
        try:
            catalog = load_route_catalog_spec(facade)
        except Exception:
            continue
        tools.update(str(route.tool or "").strip() for route in catalog.routes if str(route.tool or "").strip())
    tools.update(
        {
            "ask_project",
            "project_work",
            "project_rules",
            "project_context",
            "project_verify",
            "project_capture",
            "get",
            "submit",
            "state",
            "help",
            "mailbox_state",
            "mailbox_submit",
            "mailbox_get",
        }
    )
    return tools
