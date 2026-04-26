from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from app.models.project_task import (
    ProjectTaskRecord,
    TaskCaptureReviewRecord,
    TaskStatementActionItem,
    TaskStatementCaptureReviewView,
    TaskStatementCurrentView,
    TaskStatementDiffView,
    TaskStatementFieldEvolutionItem,
    TaskStatementProjectionResponse,
    TaskStatementQualityView,
    TaskStatementTimelineItem,
)
from app.services.improvements_store import get_improvements_store
from app.services.learning_store import get_learning_store
from app.services.project_task_service import get_project_task
from app.services.task_capture_rules import (
    collect_labeled_task_statements,
    compute_task_statement_missing_artifacts,
    looks_like_verification_evidence,
)
from app.services.text_localization import normalize_text_for_display


def _parse_timestamp(value: Any) -> datetime:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value or "").strip()
        if text:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _clean(value: Any, limit: int = 1000) -> str:
    return normalize_text_for_display(str(value or ""))[:limit].strip()


def _extract_primary_objective(task: ProjectTaskRecord, linked_improvement: dict[str, Any] | None) -> str:
    for line in str(task.description or "").splitlines():
        text = _clean(line, limit=600)
        if not text:
            continue
        if re.match(r"^\s*[-*]?\s*[A-Za-z_ ]+\s*:\s*.+$", text):
            continue
        return text
    return _clean((linked_improvement or {}).get("description") or task.description or task.title, limit=1000)


def _project_tag(project: str) -> str:
    return f"project:{project}"


def _task_tag(task_id: str) -> str:
    return f"task_id:{task_id}"


def _artifact_tags(row: dict[str, Any]) -> set[str]:
    return {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}


async def _fetch_linked_improvement(task: ProjectTaskRecord) -> dict[str, Any] | None:
    linked_id = str(task.linked_improvement_id or "").strip()
    if not linked_id:
        return None
    try:
        return await get_improvements_store().get(linked_id)
    except Exception:
        return None


async def _fetch_task_deferred_findings(*, project: str, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await get_learning_store().list_artifacts(
        artifact_type="deferred_finding",
        scope="project",
        status="active",
        limit=max(limit * 4, 100),
    )
    items: list[dict[str, Any]] = []
    project_tag = _project_tag(project)
    task_tag = _task_tag(task_id)
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        ctx = str(row.get("context_signature") or "")
        if project_tag not in tags and f"project={project}" not in ctx:
            continue
        if task_tag not in tags and f"task_id={task_id}" not in ctx:
            continue
        payload = {}
        raw_context = str(row.get("workflow_context") or "").strip()
        if raw_context:
            try:
                payload = json.loads(raw_context)
            except Exception:
                payload = {}
        items.append(
            {
                "artifact_id": str(row.get("id") or ""),
                "finding": _clean(payload.get("finding") or row.get("content") or "", limit=1000),
                "suggested_follow_up": _clean(payload.get("suggested_follow_up") or row.get("observation") or "", limit=1000),
                "why_it_matters": _clean(payload.get("why_it_matters") or row.get("why_it_matters") or "", limit=1000),
                "severity": _clean(payload.get("severity") or "medium", limit=32),
                "generated_at": _parse_timestamp(payload.get("generated_at") or row.get("created_at")),
            }
        )
    items.sort(key=lambda item: item["generated_at"])
    return items[:limit]


def _task_capture_review_record(row: dict[str, Any]) -> TaskCaptureReviewRecord:
    tags = _artifact_tags(row)
    meta = row.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    kind = next(
        (tag.split(":", 1)[1].strip() for tag in tags if tag.startswith("capture_kind:")),
        "draft",
    )
    source = next(
        (tag.split(":", 1)[1].strip() for tag in tags if tag.startswith("capture_source:")),
        "",
    )
    return TaskCaptureReviewRecord(
        kind=kind,
        content=_clean(row.get("content") or "", limit=4000),
        source=source,
        confidence=float(row.get("confidence") or 0.0),
        rationale=_clean(row.get("observation") or "", limit=1000),
        artifact_id=str(row.get("id") or ""),
        artifact_type=_clean(row.get("artifact_type") or "task_capture_candidate", limit=128),
        reused_existing=False,
        status=_clean(row.get("status") or "", limit=64),
        updated_at=_parse_timestamp(row.get("updated_at")),
        status_updated_by=_clean(meta.get("status_updated_by") or "", limit=256),
        status_update_source=_clean(meta.get("status_update_source") or "", limit=128),
        status_update_reason=_clean(meta.get("status_update_reason") or "", limit=500),
        last_review_action=_clean(meta.get("last_review_action") or "", limit=128),
    )


async def _fetch_task_capture_review(*, project: str, task_id: str, limit: int = 12) -> TaskStatementCaptureReviewView:
    store = get_learning_store()
    project_tag = _project_tag(project)
    task_tag = _task_tag(task_id)
    artifact_types = [
        "task_capture_candidate",
        "decision_candidate",
        "chosen_decision",
        "code_link",
        "remaining_risk",
    ]
    pending_rows: list[dict[str, Any]] = []
    promoted_rows: list[dict[str, Any]] = []
    for artifact_type in artifact_types:
        pending_rows.extend(
            await store.list_artifacts(
                artifact_type=artifact_type,
                scope="project",
                status="active",
                limit=max(limit * 4, 100),
            )
        )
        promoted_rows.extend(
            await store.list_artifacts(
                artifact_type=artifact_type,
                scope="project",
                status="archived",
                limit=max(limit * 4, 100),
            )
        )

    pending_candidates: list[TaskCaptureReviewRecord] = []
    promoted_candidates: list[TaskCaptureReviewRecord] = []

    for row in pending_rows:
        tags = _artifact_tags(row)
        if project_tag not in tags or task_tag not in tags:
            continue
        pending_candidates.append(_task_capture_review_record(row))
        if len(pending_candidates) >= limit:
            break

    for row in promoted_rows:
        tags = _artifact_tags(row)
        if project_tag not in tags or task_tag not in tags:
            continue
        promoted_candidates.append(_task_capture_review_record(row))
        if len(promoted_candidates) >= limit:
            break

    return TaskStatementCaptureReviewView(
        pending_count=len(pending_candidates),
        promoted_count=len(promoted_candidates),
        pending_candidates=pending_candidates,
        promoted_candidates=promoted_candidates,
    )


def _scope_summary(task: ProjectTaskRecord, *, decisions: list[str], deferred_work: list[str]) -> str:
    parts: list[str] = []
    if task.description:
        parts.append(_clean(task.description, limit=280))
    if decisions:
        parts.append(f"Chosen decisions: {len(decisions)}.")
    if deferred_work:
        parts.append(f"Deferred follow-ups: {len(deferred_work)}.")
    return " ".join(part for part in parts if part).strip() or task.title


def _field_event(
    *,
    field: str,
    value: str,
    timestamp: datetime,
    rationale: str,
    source_artifact_id: str,
    source_kind: str,
) -> dict[str, Any]:
    return {
        "field": field,
        "value": _clean(value, limit=600),
        "timestamp": timestamp,
        "rationale": _clean(rationale, limit=600),
        "source_artifact_id": str(source_artifact_id or ""),
        "source_kind": source_kind,
    }


def _append_field_events(
    events: list[dict[str, Any]],
    labeled: dict[str, list[str]],
    *,
    timestamp: datetime,
    rationale: str,
    source_artifact_id: str,
    source_kind: str,
) -> None:
    field_map = {
        "objective": "objective",
        "assumption": "assumption",
        "constraint": "constraint",
        "definition_of_done": "definition_of_done",
        "blocker": "blocker",
        "open_question": "open_question",
        "verification": "verification",
        "remaining_risk": "remaining_risk",
    }
    for source_field, target_field in field_map.items():
        for value in labeled.get(source_field, []):
            if not _clean(value, limit=600):
                continue
            events.append(
                _field_event(
                    field=target_field,
                    value=value,
                    timestamp=timestamp,
                    rationale=rationale,
                    source_artifact_id=source_artifact_id,
                    source_kind=source_kind,
                )
            )


def _build_framing_evolution(
    task: ProjectTaskRecord,
    *,
    linked_improvement: dict[str, Any] | None,
    deferred_findings: list[dict[str, Any]],
) -> list[TaskStatementFieldEvolutionItem]:
    events: list[dict[str, Any]] = []

    if linked_improvement:
        labeled = collect_labeled_task_statements(_clean(linked_improvement.get("description") or "", limit=2000))
        _append_field_events(
            events,
            labeled,
            timestamp=_parse_timestamp(linked_improvement.get("created_at")),
            rationale="Initial linked improvement framing.",
            source_artifact_id=str(linked_improvement.get("id") or ""),
            source_kind="linked_improvement",
        )

    task_labeled = collect_labeled_task_statements(task.description)
    _append_field_events(
        events,
        task_labeled,
        timestamp=task.created_at,
        rationale="Initial task framing captured in canonical task record.",
        source_artifact_id=str(task.id),
        source_kind="task_created",
    )

    for change in task.changes:
        labeled = collect_labeled_task_statements(change.content, change.why)
        _append_field_events(
            events,
            labeled,
            timestamp=change.timestamp,
            rationale=change.why,
            source_artifact_id=str(change.id),
            source_kind=f"task_change:{change.change_type}",
        )

    for finding in deferred_findings:
        open_questions: list[str] = []
        suggested = _clean(finding.get("suggested_follow_up") or "", limit=400)
        if "?" in suggested:
            open_questions.append(suggested)
        why = _clean(finding.get("why_it_matters") or "", limit=400)
        if "?" in why:
            open_questions.append(why)
        for value in open_questions:
            events.append(
                _field_event(
                    field="open_question",
                    value=value,
                    timestamp=finding["generated_at"],
                    rationale=finding.get("why_it_matters") or finding.get("suggested_follow_up") or "",
                    source_artifact_id=str(finding.get("artifact_id") or ""),
                    source_kind="deferred_finding",
                )
            )

    seen_pairs: set[tuple[str, str]] = set()
    evolution: list[TaskStatementFieldEvolutionItem] = []
    for event in sorted(events, key=lambda item: item["timestamp"]):
        pair = (event["field"], event["value"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        evolution.append(
            TaskStatementFieldEvolutionItem(
                field=event["field"],
                value=event["value"],
                timestamp=event["timestamp"],
                rationale=event["rationale"],
                source_artifact_id=event["source_artifact_id"],
                source_kind=event["source_kind"],
            )
        )
    return evolution


def _build_next_actions(
    *,
    quality: TaskStatementQualityView,
    capture_review: TaskStatementCaptureReviewView,
    blockers: list[str],
    open_questions: list[str],
    deferred_work: list[str],
) -> list[TaskStatementActionItem]:
    actions: list[TaskStatementActionItem] = []

    for missing in quality.missing_artifacts:
        actions.append(
            TaskStatementActionItem(
                priority="high",
                action=f"Capture missing {missing.replace('_', ' ')} as a grounded task artifact.",
                rationale="The task statement is still incomplete and cannot serve as a reliable handoff or active-task view.",
                source_kind="missing_artifact",
            )
        )

    if capture_review.pending_count > 0:
        actions.append(
            TaskStatementActionItem(
                priority="high" if capture_review.pending_count >= 3 else "medium",
                action=f"Review {capture_review.pending_count} pending task capture draft(s) and promote or reject them.",
                rationale="Unreviewed capture drafts keep the framing provisional and can hide duplicates or unresolved alternatives.",
                source_kind="capture_review",
            )
        )

    if blockers:
        actions.append(
            TaskStatementActionItem(
                priority="high",
                action=f"Resolve blocker: {blockers[0]}",
                rationale="Active blockers should be surfaced before additional execution work continues.",
                source_kind="blocker",
            )
        )

    if open_questions:
        actions.append(
            TaskStatementActionItem(
                priority="medium",
                action=f"Answer open question: {open_questions[0]}",
                rationale="Unresolved ambiguities weaken task framing and can cause drift in later task changes.",
                source_kind="open_question",
            )
        )

    if deferred_work:
        actions.append(
            TaskStatementActionItem(
                priority="medium",
                action=f"Triage deferred follow-up: {deferred_work[0]}",
                rationale="Deferred findings often indicate missing governance, contradiction handling, or verification follow-up.",
                source_kind="deferred_finding",
            )
        )

    if not actions and quality.capture_quality == "complete":
        actions.append(
            TaskStatementActionItem(
                priority="low",
                action="Proceed with implementation using the current task statement as the active framing source.",
                rationale="The task framing is complete and no pending review work is blocking execution.",
                source_kind="ready",
            )
        )

    deduped: list[TaskStatementActionItem] = []
    seen: set[tuple[str, str]] = set()
    for item in actions:
        key = (item.priority, item.action)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:5]


def _quality_view(
    task: ProjectTaskRecord,
    *,
    linked_improvement: dict[str, Any] | None,
    decisions: list[str],
    verification: list[str],
    deferred_findings: list[dict[str, Any]],
) -> TaskStatementQualityView:
    missing = compute_task_statement_missing_artifacts(
        title=task.title,
        description=task.description,
        status=task.status,
        changes=task.changes,
    )
    grounded_by: list[str] = []

    if task.description:
        grounded_by.append("task_description")

    if task.changes:
        grounded_by.append("task_changes")

    if linked_improvement:
        grounded_by.append("linked_improvement")

    if decisions:
        grounded_by.append("decision_trace")

    if deferred_findings:
        grounded_by.append("deferred_findings")

    if task.status == "done":
        if verification:
            grounded_by.append("verification_result")

    if not missing:
        quality = "complete"
    elif grounded_by:
        quality = "partial"
    else:
        quality = "weak"

    return TaskStatementQualityView(
        capture_quality=quality,
        missing_artifacts=missing,
        grounded_by=grounded_by,
    )


async def build_task_statement_projection(qdrant, *, project: str, task_id: str) -> TaskStatementProjectionResponse:
    task = await get_project_task(qdrant, project=project, task_id=task_id, include_changes=True)
    linked_improvement = await _fetch_linked_improvement(task)
    deferred_findings = await _fetch_task_deferred_findings(project=project, task_id=task_id)
    capture_review = await _fetch_task_capture_review(project=project, task_id=task_id)

    initial_objective = _extract_primary_objective(task, linked_improvement)

    collected = collect_labeled_task_statements(
        task.description,
        *[change.content for change in task.changes],
        *[change.why for change in task.changes],
        *[item.get("finding") or "" for item in deferred_findings],
        *[item.get("suggested_follow_up") or "" for item in deferred_findings],
        *[item.get("why_it_matters") or "" for item in deferred_findings],
    )
    collected.setdefault("blocker", [])
    collected.setdefault("open_question", [])

    decisions = []
    execution_changes = []
    verification = list(collected["verification"])
    for change in task.changes:
        text = _clean(change.content, limit=500)
        if not text:
            continue
        if change.change_type == "decision" and text not in decisions:
            decisions.append(text)
        if change.change_type in {"implementation", "status_change"} and text not in execution_changes:
            execution_changes.append(text)
        if looks_like_verification_evidence(text):
            if text not in verification:
                verification.append(text)

    deferred_work = [_clean(item["finding"], limit=400) for item in deferred_findings if _clean(item["finding"], limit=400)]
    blockers = list(collected["blocker"])
    open_questions = list(collected["open_question"])

    if not blockers:
        blockers.extend(item for item in deferred_work if "block" in item.lower() or "fail" in item.lower())
    if not open_questions:
        open_questions.extend(item for item in deferred_work if "?" in item)
    framing_evolution = _build_framing_evolution(
        task,
        linked_improvement=linked_improvement,
        deferred_findings=deferred_findings,
    )
    unresolved_ambiguities = list(dict.fromkeys([
        *open_questions,
        *[
            f"Missing artifact: {item.replace('_', ' ')}"
            for item in compute_task_statement_missing_artifacts(
                title=task.title,
                description=task.description,
                status=task.status,
                changes=task.changes,
            )
        ],
        *[
            item.content
            for item in capture_review.pending_candidates
            if item.kind in {"decision_candidate", "open_question"}
        ],
    ]))

    current_objective = initial_objective
    if collected["objective"]:
        current_objective = collected["objective"][-1]

    timeline: list[TaskStatementTimelineItem] = []
    if linked_improvement:
        timeline.append(
            TaskStatementTimelineItem(
                kind="linked_improvement",
                timestamp=_parse_timestamp(linked_improvement.get("created_at")),
                title=_clean(linked_improvement.get("title") or "Linked improvement", limit=200),
                detail=_clean(linked_improvement.get("description") or "", limit=1000),
                rationale="Initial project pressure or requirement linked to this task.",
                source_artifact_id=str(linked_improvement.get("id") or ""),
                inferred=False,
            )
        )

    timeline.append(
        TaskStatementTimelineItem(
            kind="task_created",
            timestamp=task.created_at,
            title=task.title,
            detail=_clean(task.description, limit=1000),
            rationale="Initial task framing captured in canonical task record.",
            source_artifact_id=str(task.id),
            inferred=False,
        )
    )

    for change in task.changes:
        timeline.append(
            TaskStatementTimelineItem(
                kind=f"task_change:{change.change_type}",
                timestamp=change.timestamp,
                title=_clean(change.change_type.replace("_", " ").title(), limit=120),
                detail=_clean(change.content, limit=1000),
                rationale=_clean(change.why, limit=600),
                source_artifact_id=str(change.id),
                inferred=False,
            )
        )

    for finding in deferred_findings:
        timeline.append(
            TaskStatementTimelineItem(
                kind="deferred_finding",
                timestamp=finding["generated_at"],
                title=_clean(f"Deferred ({finding['severity']})", limit=120),
                detail=_clean(finding["finding"], limit=1000),
                rationale=_clean(finding["why_it_matters"] or finding["suggested_follow_up"], limit=600),
                source_artifact_id=str(finding["artifact_id"]),
                inferred=False,
            )
        )

    timeline.sort(key=lambda item: item.timestamp)

    current = TaskStatementCurrentView(
        objective=current_objective or task.title,
        scope_summary=_scope_summary(task, decisions=decisions, deferred_work=deferred_work),
        assumptions=collected["assumption"],
        constraints=collected["constraint"],
        definition_of_done=collected["definition_of_done"],
        chosen_decisions=decisions,
        blockers=blockers,
        open_questions=open_questions,
        deferred_work=deferred_work,
        verification=verification,
    )

    diff = TaskStatementDiffView(
        original_objective=initial_objective or task.title,
        current_objective=current.objective,
        new_decisions=decisions,
        changed_constraints=collected["constraint"],
        newly_deferred=deferred_work,
        execution_changes=execution_changes,
        framing_evolution=framing_evolution,
        unresolved_ambiguities=unresolved_ambiguities,
        changed=bool(
            decisions
            or collected["constraint"]
            or deferred_work
            or execution_changes
            or framing_evolution
            or current.objective != (initial_objective or task.title)
        ),
    )

    quality = _quality_view(
        task,
        linked_improvement=linked_improvement,
        decisions=decisions,
        verification=verification,
        deferred_findings=deferred_findings,
    )
    next_actions = _build_next_actions(
        quality=quality,
        capture_review=capture_review,
        blockers=blockers,
        open_questions=open_questions,
        deferred_work=deferred_work,
    )

    return TaskStatementProjectionResponse(
        task=task,
        current=current,
        timeline=timeline,
        diff=diff,
        quality=quality,
        capture_review=capture_review,
        next_actions=next_actions,
    )
