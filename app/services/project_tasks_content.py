from __future__ import annotations

from app.services.text_localization import normalize_text_for_display


def build_task_content(title: str, description: str) -> str:
    """Normalize title/description and concatenate for task memories."""
    title_text = normalize_text_for_display(title)
    description_text = normalize_text_for_display(description)
    if not description_text:
        return title_text
    return f"{title_text}\n\n{description_text}"


def build_task_change_content(change_type: str, content: str, why: str) -> str:
    """Normalize change details and format them for storing task_change memories."""
    cleaned_change = normalize_text_for_display(content)
    cleaned_why = normalize_text_for_display(why)
    lines = [f"[{change_type}] {cleaned_change}"]
    if cleaned_why:
        lines.append(f"[why] {cleaned_why}")
    return "\n".join(lines)


def build_task_checkpoint_content(
    *,
    stage: str,
    status: str,
    summary: str,
    blockers: list[str] | None = None,
    decisions: list[str] | None = None,
    changed_files: list[str] | None = None,
    verification: list[str] | None = None,
    remaining_risk: list[str] | None = None,
    next_step: str = "",
    next_step_scope: str = "",
    stage_evidence_refs: list[str] | None = None,
    reason: str = "",
) -> str:
    """Format a compact stage checkpoint for task_change storage."""
    cleaned_stage = normalize_text_for_display(stage).strip() or "unknown"
    cleaned_status = normalize_text_for_display(status).strip() or "planning"
    cleaned_summary = normalize_text_for_display(summary).strip()
    cleaned_next_step = normalize_text_for_display(next_step).strip()
    cleaned_reason = normalize_text_for_display(reason).strip()
    cleaned_blockers = [
        normalize_text_for_display(item).strip()
        for item in (blockers or [])
        if normalize_text_for_display(item).strip()
    ]
    cleaned_decisions = [
        normalize_text_for_display(item).strip()
        for item in (decisions or [])
        if normalize_text_for_display(item).strip()
    ]
    cleaned_changed_files = [
        normalize_text_for_display(item).strip()
        for item in (changed_files or [])
        if normalize_text_for_display(item).strip()
    ]
    cleaned_verification = [
        normalize_text_for_display(item).strip()
        for item in (verification or [])
        if normalize_text_for_display(item).strip()
    ]
    cleaned_remaining_risk = [
        normalize_text_for_display(item).strip()
        for item in (remaining_risk or [])
        if normalize_text_for_display(item).strip()
    ]
    cleaned_next_step_scope = normalize_text_for_display(next_step_scope).strip()
    cleaned_stage_evidence_refs = [
        normalize_text_for_display(item).strip()
        for item in (stage_evidence_refs or [])
        if normalize_text_for_display(item).strip()
    ]

    lines = ["[task_checkpoint]", f"Checkpoint stage: {cleaned_stage}", f"Checkpoint status: {cleaned_status}"]
    if cleaned_summary:
        lines.append(f"Summary: {cleaned_summary}")
    if cleaned_blockers:
        lines.append("Blockers: " + "; ".join(cleaned_blockers))
    if cleaned_decisions:
        lines.append("Decisions: " + "; ".join(cleaned_decisions))
    if cleaned_changed_files:
        lines.append("Changed files: " + "; ".join(cleaned_changed_files))
    if cleaned_verification:
        lines.append("Verification: " + "; ".join(cleaned_verification))
    if cleaned_remaining_risk:
        lines.append("Remaining risk: " + "; ".join(cleaned_remaining_risk))
    if cleaned_next_step:
        lines.append(f"Next step: {cleaned_next_step}")
    if cleaned_next_step_scope:
        lines.append(f"Next step scope: {cleaned_next_step_scope}")
    if cleaned_stage_evidence_refs:
        lines.append("Stage evidence refs: " + "; ".join(cleaned_stage_evidence_refs))
    if cleaned_reason:
        lines.append(f"Reason: {cleaned_reason}")
    return "\n".join(lines)
