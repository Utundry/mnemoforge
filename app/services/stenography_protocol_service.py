from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.stenographer import STENOGRAPHER_KIND_PATTERN
from app.services.mcp_workflow_specs import load_named_json_spec


def _spec() -> dict[str, Any]:
    return load_named_json_spec("workflow/stenography_protocol.json")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _scope_fragment(*, task_id: str = "", work_id: str = "") -> str:
    parts = []
    if task_id:
        parts.append(f"task_id={task_id}")
    if work_id:
        parts.append(f"work_id={work_id}")
    return " " + " ".join(parts) if parts else " task_id=<task_id>"


def _snippet(kind: str, body: str, *, task_id: str = "", work_id: str = "") -> dict[str, str]:
    scope = _scope_fragment(task_id=task_id, work_id=work_id)
    return {
        "kind": kind,
        "text": f"[stenographer:start kind={kind}{scope}]\n{body}\n[stenographer:stop]",
    }


def _snippets_from_spec(spec: dict[str, Any], *, task_id: str = "", work_id: str = "") -> list[dict[str, str]]:
    snippets = []
    for item in spec.get("snippets") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        body = str(item.get("body") or "").strip()
        if kind and body:
            snippets.append(_snippet(kind, body, task_id=task_id, work_id=work_id))
    return snippets


def build_stenography_protocol(*, project: str = "", task_id: str = "", work_id: str = "", state: str = "") -> dict[str, Any]:
    """Public, weak-model-safe instructions for the tag-driven stenographer protocol."""

    spec = _spec()
    return {
        "status": str(spec.get("status") or "").strip(),
        "capture_model": str(spec.get("capture_model") or "").strip(),
        "why": str(spec.get("why") or "").strip(),
        "project": project,
        "task_id": task_id,
        "work_id": work_id,
        "state": state,
        "supported_span_kinds": _string_list(spec.get("supported_span_kinds")),
        "core_recovery_span_kinds": _string_list(spec.get("core_recovery_span_kinds")),
        "minimum_closeout_span_kinds": _string_list(spec.get("minimum_closeout_span_kinds")),
        "kind_pattern": STENOGRAPHER_KIND_PATTERN,
        "markers": dict(spec.get("markers") or {}),
        "snippets": _snippets_from_spec(spec, task_id=task_id, work_id=work_id),
        "clerk_rules": dict(spec.get("clerk_rules") or {}),
        "validation_checklist": _string_list(spec.get("validation_checklist")),
        "next_safe_action": str(spec.get("next_safe_action") or "").strip(),
    }


def build_stenography_coverage(
    *,
    project: str,
    task_id: str,
    work_id: str = "",
    agent_id: str = "",
    session_id: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    spec = _spec()
    messages = spec.get("coverage_messages") if isinstance(spec.get("coverage_messages"), dict) else {}
    minimum_closeout_kinds = _string_list(spec.get("minimum_closeout_span_kinds"))
    task_id = str(task_id or "").strip()
    if not task_id:
        return {
            "status": "unscoped",
            "span_count": 0,
            "next_safe_action": str(messages.get("unscoped_next_safe_action") or "").strip(),
        }
    try:
        from app.services.stenographer_service import get_stenographer_store

        spans = get_stenographer_store().list_spans(
            project=str(project or "") or None,
            task_id=task_id,
            work_id=str(work_id or "") or None,
            agent_id=str(agent_id or "") or None,
            session_id=str(session_id or "") or None,
            limit=max(1, min(100, int(limit or 50))),
        )
    except Exception as exc:
        return {
            "status": "unknown",
            "span_count": 0,
            "error": type(exc).__name__,
            "next_safe_action": str(messages.get("unknown_next_safe_action") or "").strip(),
        }

    by_kind = Counter(str(getattr(span, "kind", "") or "") for span in spans)
    status = "present" if spans else "none"
    coverage = {
        "status": status,
        "span_count": len(spans),
        "by_kind": {key: by_kind[key] for key in sorted(by_kind) if key},
        "has_changed_files": bool(by_kind.get("changed_files")),
        "has_verification": bool(by_kind.get("verification") or by_kind.get("diagnostic")),
        "has_decision": bool(by_kind.get("decision")),
        "has_risk_or_blocker": bool(by_kind.get("risk") or by_kind.get("blocker")),
    }
    missing_closeout_kinds = [kind for kind in minimum_closeout_kinds if not by_kind.get(kind)]
    coverage["minimum_closeout_span_kinds"] = minimum_closeout_kinds
    coverage["missing_closeout_span_kinds"] = missing_closeout_kinds
    if spans and not missing_closeout_kinds:
        coverage["next_safe_action"] = str(messages.get("complete_next_safe_action") or "").strip()
    elif spans:
        coverage["warning"] = str(messages.get("incomplete_warning") or "").strip()
        coverage["next_safe_action"] = str(messages.get("incomplete_next_safe_action") or "").strip()
    else:
        coverage["warning"] = str(messages.get("none_warning") or "").strip()
        coverage["next_safe_action"] = str(messages.get("none_next_safe_action") or "").strip()
    return coverage


def stenography_supported_by_forms(forms: list[Any]) -> bool:
    for form in forms:
        assistance = getattr(form, "assistance", None)
        if assistance and bool(getattr(assistance, "can_use_stenography", False)):
            return True
    return False