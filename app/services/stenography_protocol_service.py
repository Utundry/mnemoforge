from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.stenographer import STENOGRAPHER_KIND_PATTERN


SUPPORTED_SPAN_KINDS = (
    "fact",
    "decision",
    "verification",
    "risk",
    "blocker",
    "next_step",
    "checkpoint_hint",
    "handoff_hint",
    "diagnostic",
    "changed_files",
    "rule_project_candidate",
    "rule_canonical_candidate",
    "rule_revision_hint",
    "rule_merge_hint",
)

CORE_RECOVERY_SPAN_KINDS = (
    "checkpoint_hint",
    "changed_files",
    "decision",
    "verification",
    "risk",
    "blocker",
    "next_step",
    "handoff_hint",
    "diagnostic",
)


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


def build_stenography_protocol(*, project: str = "", task_id: str = "", work_id: str = "", state: str = "") -> dict[str, Any]:
    """Public, weak-model-safe instructions for the tag-driven stenographer protocol."""

    return {
        "status": "available",
        "capture_model": "tagged_spans",
        "why": (
            "Stenographer capture is tag-driven. Clerk draft quality depends on explicit tagged spans; "
            "without spans, clerk can only infer from sparse task/checkpoint text."
        ),
        "project": project,
        "task_id": task_id,
        "work_id": work_id,
        "state": state,
        "supported_span_kinds": list(SUPPORTED_SPAN_KINDS),
        "core_recovery_span_kinds": list(CORE_RECOVERY_SPAN_KINDS),
        "kind_pattern": STENOGRAPHER_KIND_PATTERN,
        "markers": {
            "start": "[stenographer:start kind=<kind> task_id=<task_id>]",
            "stop": "[stenographer:stop]",
            "rule": "Every start marker needs one matching stop marker. Keep one span focused on one evidence type.",
        },
        "snippets": [
            _snippet(
                "checkpoint_hint",
                "Summary: <implementation slice completed>\nWhat changed: <short factual summary>",
                task_id=task_id,
                work_id=work_id,
            ),
            _snippet(
                "changed_files",
                "Files: app/path.py; tests/test_path.py",
                task_id=task_id,
                work_id=work_id,
            ),
            _snippet(
                "decision",
                "Decision: <chosen path>\nRationale: <why>\nAlternatives rejected: <if any>",
                task_id=task_id,
                work_id=work_id,
            ),
            _snippet(
                "verification",
                "Command: <command actually run>\nResult: <pass/fail/output summary>\nResidual risk: <if any>",
                task_id=task_id,
                work_id=work_id,
            ),
            _snippet(
                "risk",
                "Risk: <known uncertainty>\nMitigation/next check: <concrete follow-up>",
                task_id=task_id,
                work_id=work_id,
            ),
            _snippet(
                "handoff_hint",
                "Next step: <concrete continuation>\nDo not forget: <important constraint>",
                task_id=task_id,
                work_id=work_id,
            ),
        ],
        "clerk_rules": {
            "draft_only": True,
            "draft_tool": "clerk_draft_report",
            "span_draft_tool": "draft_checkpoint_from_spans",
            "approve_tool": "approve_checkpoint_draft",
            "rule": "Clerk output is a review-only draft until an agent/operator validates and approves it.",
        },
        "validation_checklist": [
            "summary matches actual work",
            "changed_files are grounded in real touched files",
            "verification commands/results were actually run",
            "decisions and rejected alternatives are not invented",
            "risks/blockers are preserved",
            "next_step is concrete",
            "formal verification is distinct from live diagnostic/operator feedback",
        ],
        "next_safe_action": "Capture short tagged spans after each meaningful work slice; ask clerk for a draft only after spans exist.",
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
    task_id = str(task_id or "").strip()
    if not task_id:
        return {
            "status": "unscoped",
            "span_count": 0,
            "next_safe_action": "Provide task_id to assess stenographer span coverage for recovery.",
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
            "next_safe_action": "Stenographer span coverage could not be read; continue with explicit checkpoint fields.",
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
    if spans:
        coverage["next_safe_action"] = "Use clerk_draft_report or draft_checkpoint_from_spans, then validate the review-only draft before checkpointing."
    else:
        coverage["warning"] = "Stenographer is available but no tagged stenographer spans were found for this task; clerk draft quality will be low."
        coverage["next_safe_action"] = "Before checkpoint/finish, add short tagged spans for summary, changed_files, verification, and next_step."
    return coverage


def stenography_supported_by_forms(forms: list[Any]) -> bool:
    for form in forms:
        assistance = getattr(form, "assistance", None)
        if assistance and bool(getattr(assistance, "can_use_stenography", False)):
            return True
    return False