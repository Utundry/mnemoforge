from __future__ import annotations

from typing import Any

from app.services.diagnostic_inspection_service import build_diagnostic_inspection_packet
from app.services.learning_eligibility_service import evaluate_learning_eligibility


_VALID_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_VALID_AREAS = {
    "routing",
    "learning",
    "hygiene",
    "public_ref",
    "lifecycle",
    "mcp_surface",
    "storage",
    "other",
}


def build_developer_feedback_packet(
    *,
    project: str,
    payload: dict[str, Any],
    diagnostic: bool = False,
) -> dict[str, Any]:
    area = _normalized_choice(payload.get("area"), _VALID_AREAS, default="other")
    severity = _normalized_choice(payload.get("severity"), _VALID_SEVERITIES, default="medium")
    title = _clean_text(payload.get("title"), fallback=_default_title(area))
    observed = _clean_text(payload.get("observed_behavior") or payload.get("observed"), fallback="")
    expected = _clean_text(payload.get("expected_behavior") or payload.get("expected"), fallback="")
    impact = _clean_text(payload.get("impact"), fallback="")
    next_action = _clean_text(payload.get("next_action"), fallback=_default_next_action(severity))
    evidence_refs = _string_list(payload.get("evidence_refs"))[:20]
    reproduction_steps = _string_list(payload.get("reproduction_steps"))[:20]

    missing = [
        field
        for field, value in (
            ("observed_behavior", observed),
            ("expected_behavior", expected),
        )
        if not value
    ]
    diagnostic_packet = None
    if bool(payload.get("include_diagnostic", True)):
        diagnostic_payload = payload.get("diagnostic_payload")
        if not isinstance(diagnostic_payload, dict):
            diagnostic_payload = _diagnostic_payload_from_feedback(payload, area=area)
        diagnostic_packet = build_diagnostic_inspection_packet(
            project=project,
            payload=diagnostic_payload,
            diagnostic=diagnostic,
        )

    learning_guardrail = evaluate_learning_eligibility(
        source=str(payload.get("source") or "problem_report"),
        metadata={
            "source_event_class": "problem_report",
            "diagnostic": True,
            "do_not_train": True,
            "project": project,
            "area": area,
        },
        pattern=" ".join([title, observed, expected]),
    )
    packet = {
        "status": "ready" if not missing else "needs_input",
        "read_only": True,
        "auto_submitted": False,
        "project": project,
        "area": area,
        "severity": severity,
        "title": title,
        "missing_fields": missing,
        "report": {
            "observed_behavior": observed,
            "expected_behavior": expected,
            "impact": impact,
            "reproduction_steps": reproduction_steps,
            "evidence_refs": evidence_refs,
            "next_action": next_action,
        },
        "learning_guardrail": {
            "eligible": bool(learning_guardrail.get("eligible")),
            "decision": learning_guardrail.get("decision"),
            "reason": learning_guardrail.get("reason"),
        },
        "developer_summary": _developer_summary(
            project=project,
            area=area,
            severity=severity,
            title=title,
            observed=observed,
            expected=expected,
            impact=impact,
            evidence_refs=evidence_refs,
            reproduction_steps=reproduction_steps,
            diagnostic_packet=diagnostic_packet,
        ),
        "next_safe_action": (
            "Fill the missing fields before sending this packet to developers."
            if missing
            else "Review the packet, then send it to the project maintainer or create an improvement if the operator approves."
        ),
    }
    if diagnostic_packet:
        packet["diagnostic_summary"] = {
            "status": diagnostic_packet.get("status"),
            "target": diagnostic_packet.get("target"),
            "read_only": bool(diagnostic_packet.get("read_only")),
            "summary": diagnostic_packet.get("summary"),
            "findings": diagnostic_packet.get("findings", [])[:5],
            "next_diagnostic_action": diagnostic_packet.get("next_diagnostic_action"),
        }
        if diagnostic:
            packet["diagnostic_packet"] = diagnostic_packet
    return packet


def _diagnostic_payload_from_feedback(payload: dict[str, Any], *, area: str) -> dict[str, Any]:
    target = {
        "routing": "routing",
        "learning": "learning",
        "public_ref": "public_ref",
    }.get(area, "all")
    return {
        "target": target,
        "facade": payload.get("facade"),
        "query": payload.get("query"),
        "ref": payload.get("ref"),
        "artifact_type": payload.get("artifact_type"),
        "source": "problem_report",
        "metadata": {
            "source_event_class": "problem_report",
            "diagnostic": True,
            "do_not_train": True,
        },
        "limit": payload.get("limit") or 10,
    }


def _developer_summary(
    *,
    project: str,
    area: str,
    severity: str,
    title: str,
    observed: str,
    expected: str,
    impact: str,
    evidence_refs: list[str],
    reproduction_steps: list[str],
    diagnostic_packet: dict[str, Any] | None,
) -> str:
    lines = [
        f"Project: {project}",
        f"Area: {area}",
        f"Severity: {severity}",
        f"Title: {title}",
    ]
    if observed:
        lines.append(f"Observed: {observed}")
    if expected:
        lines.append(f"Expected: {expected}")
    if impact:
        lines.append(f"Impact: {impact}")
    if reproduction_steps:
        lines.append("Reproduction steps:")
        lines.extend(f"- {item}" for item in reproduction_steps)
    if evidence_refs:
        lines.append("Evidence refs:")
        lines.extend(f"- {item}" for item in evidence_refs)
    if diagnostic_packet:
        summary = diagnostic_packet.get("summary") if isinstance(diagnostic_packet.get("summary"), dict) else {}
        lines.append(
            "Diagnostic summary: "
            f"target={diagnostic_packet.get('target')} "
            f"findings={summary.get('findings', 0)} "
            f"likely_source={summary.get('likely_source', 'unknown')}"
        )
    return "\n".join(lines)


def _clean_text(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [line.strip(" -") for line in value.splitlines() if line.strip(" -")]
    return []


def _normalized_choice(value: Any, choices: set[str], *, default: str) -> str:
    clean = str(value or "").strip().casefold()
    return clean if clean in choices else default


def _default_title(area: str) -> str:
    return f"Unresolved {area} issue"


def _default_next_action(severity: str) -> str:
    if severity in {"critical", "high"}:
        return "Ask the maintainer to review the packet before continuing risky workflow."
    return "Review the packet and decide whether it should become an improvement task."
