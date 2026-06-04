from __future__ import annotations

from typing import Any

from app.services.context_cue_service import context_cues_for_query, context_cues_for_state
from app.services.mcp_workflow_specs import load_named_json_spec


def _cognitive_health_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/cognitive_health.json")
    except Exception:
        return {"default_max_checks": 0, "checks": []}


def build_cognitive_health_packet(
    *,
    project: str,
    state: str,
    query: str = "",
    limit: int | None = None,
) -> dict[str, Any]:
    state_text = str(state or "planning").strip() or "planning"
    max_checks = limit or int(_cognitive_health_spec().get("default_max_checks") or 5)
    checks = [
        _public_check(check)
        for check in _cognitive_health_spec().get("checks") or []
        if isinstance(check, dict) and _check_applies(check, state=state_text)
    ][:max_checks]
    cues = context_cues_for_query(query=query, project=project, state=state_text, max_cues=max_checks)
    if not cues:
        cues = context_cues_for_state(state=state_text, project=project, max_cues=max_checks)
    return {
        "status": "needs_self_check",
        "project": project,
        "state": state_text,
        "health_score": None,
        "evaluator_executed": False,
        "read_only": True,
        "checks": checks,
        "context_cues": cues,
        "next_safe_action": "Answer the self-check questions briefly; expand cue refs only when recall is insufficient.",
    }


def build_health_nudge(*, project: str, state: str, query: str = "", limit: int = 1) -> dict[str, Any]:
    state_text = str(state or "planning").strip() or "planning"
    packet = build_cognitive_health_packet(project=project, state=state_text, query=query, limit=max(5, limit))
    checks = sorted(
        [
            check
            for check in _cognitive_health_spec().get("checks") or []
            if isinstance(check, dict) and _check_applies(check, state=state_text)
        ],
        key=lambda check: _check_nudge_priority(check),
        reverse=True,
    )
    cues = [cue for cue in packet.get("context_cues") or [] if isinstance(cue, dict)]
    check = _public_check(checks[0]) if checks else {}
    cue = _first_expected_or_available_cue(check=check, cues=cues)
    if not check:
        return {}
    return {
        key: value
        for key, value in {
            "reason": f"state:{str(state or 'planning').strip() or 'planning'}",
            "check": check.get("question"),
            "severity": check.get("severity"),
            "cue": cue.get("cue") if isinstance(cue, dict) else "",
            "summary": cue.get("summary") if isinstance(cue, dict) else "",
            "expand_ref": cue.get("expand_ref") if isinstance(cue, dict) else "",
            "next_safe_action": "Answer this self-check briefly; expand the cue only if recall is insufficient.",
        }.items()
        if value not in (None, "", [], {})
    }


def _check_nudge_priority(check: dict[str, Any]) -> float:
    try:
        return float(check.get("nudge_priority") or 0.0)
    except Exception:
        return 0.0


def _first_expected_or_available_cue(*, check: dict[str, Any], cues: list[dict[str, Any]]) -> dict[str, Any]:
    expected = [str(item or "").strip() for item in check.get("expected_cues") or []]
    for cue_id in expected:
        for cue in cues:
            if str(cue.get("cue") or "").strip() == cue_id:
                return cue
    return cues[0] if cues else {}


def _check_applies(check: dict[str, Any], *, state: str) -> bool:
    scopes = {str(item or "").strip() for item in check.get("stage_scope") or []}
    return not scopes or state in scopes


def _public_check(check: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "id": check.get("id"),
            "severity": check.get("severity"),
            "question": check.get("question"),
            "expected_cues": check.get("expected_cues") or [],
            "risk_if_failed": check.get("risk_if_failed"),
        }.items()
        if value not in (None, "", [], {})
    }
