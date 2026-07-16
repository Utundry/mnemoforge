from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.artifact_lifecycle import (
    ArtifactLifecycleReconcileResponse,
    ArtifactLifecycleScopeReviewBatchRequest,
    ArtifactLifecycleScopeReviewDecision,
    CompletedCheckpointArtifactCandidate,
    LifecycleAnomalyRepairCandidate,
    LifecycleAnomalyRepairResponse,
)
from app.models.unified_artifact import UnifiedArtifactResolveRequest
from app.services.project_tasks_store import get_project_tasks_store
from app.services.unified_artifact_service import get_unified_artifact_service


_CHECKPOINT_TAG = "task_checkpoint"
_SCOPE_REVIEW_TAG = "task_checkpoint_scope_review"
_OPEN_TASK_STATUSES = {"planning", "active", "paused"}
_NEXT_STEP_SCOPES = {"none", "follow_up_task", "same_artifact_remaining_work", "operator_review", "unknown"}
_REVIEWABLE_NEXT_STEP_SCOPES = {"none", "follow_up_task", "same_artifact_remaining_work", "operator_review"}


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _parse_checkpoint_change(change: dict[str, Any]) -> dict[str, Any] | None:
    tags = {str(tag).strip() for tag in (change.get("tags") or []) if str(tag).strip()}
    content = str(change.get("content") or "")
    if _CHECKPOINT_TAG not in tags and "[task_checkpoint]" not in content:
        return None

    parsed: dict[str, Any] = {
        "id": str(change.get("id") or ""),
        "timestamp": datetime.fromtimestamp(float(change["created_at"]), tz=timezone.utc),
        "stage": "",
        "status": "",
        "summary": "",
        "blockers": [],
        "remaining_risk": [],
        "next_step": "",
        "next_step_scope": "unknown",
        "next_step_scope_source": "absent",
    }
    scalar_fields = {
        "Checkpoint stage": "stage",
        "Checkpoint status": "status",
        "Summary": "summary",
        "Next step": "next_step",
        "Next step scope": "next_step_scope",
    }
    list_fields = {
        "Blockers": "blockers",
        "Remaining risk": "remaining_risk",
    }
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in scalar_fields:
            parsed[scalar_fields[key]] = value
        elif key in list_fields:
            parsed[list_fields[key]] = _split_list(value)
    return parsed


def build_checkpoint_scope_review_content(
    *,
    checkpoint_change_id: str = "",
    next_step_scope: str,
    reason: str = "",
) -> str:
    lines = ["[task_checkpoint_scope_review]"]
    if checkpoint_change_id:
        lines.append(f"Target checkpoint: {checkpoint_change_id}")
    lines.append(f"Next step scope: {_normalize_next_step_scope(next_step_scope)}")
    if reason:
        lines.append(f"Reason: {reason.strip()}")
    return "\n".join(lines)


def _parse_scope_review_change(change: dict[str, Any]) -> dict[str, Any] | None:
    tags = {str(tag).strip() for tag in (change.get("tags") or []) if str(tag).strip()}
    content = str(change.get("content") or "")
    if _SCOPE_REVIEW_TAG not in tags and "[task_checkpoint_scope_review]" not in content:
        return None
    parsed: dict[str, Any] = {
        "id": str(change.get("id") or ""),
        "timestamp": datetime.fromtimestamp(float(change["created_at"]), tz=timezone.utc),
        "target_checkpoint_id": "",
        "next_step_scope": "unknown",
    }
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key == "Target checkpoint":
            parsed["target_checkpoint_id"] = value
        elif key == "Next step scope":
            parsed["next_step_scope"] = _normalize_next_step_scope(value)
    if parsed["next_step_scope"] == "unknown":
        return None
    return parsed


def _normalize_next_step_scope(value: str) -> str:
    scope = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return scope if scope in _NEXT_STEP_SCOPES else "unknown"


def _infer_next_step_scope(next_step: str) -> tuple[str, str]:
    text = str(next_step or "").strip().casefold()
    if not text:
        return "none", "absent"
    follow_up_markers = (
        "next slice",
        "later",
        "separate",
        "follow-up",
        "follow up",
        "after server restart",
        "затем",
        "следующая задача",
        "следующий срез",
        "позже",
    )
    same_artifact_markers = (
        "finish",
        "complete",
        "continue implementing",
        "remaining work",
        "blocker",
        "доделать",
        "продолжить реализацию",
        "оставшаяся работа",
    )
    review_markers = ("review", "inspect", "manual", "operator", "посмотреть", "проверить", "разобрать")
    if any(marker in text for marker in same_artifact_markers):
        return "same_artifact_remaining_work", "inferred"
    if any(marker in text for marker in follow_up_markers):
        return "follow_up_task", "inferred"
    if any(marker in text for marker in review_markers):
        return "operator_review", "inferred"
    return "unknown", "inferred"


def _is_completed_checkpoint(checkpoint: dict[str, Any]) -> bool:
    return (
        str(checkpoint.get("stage") or "").strip().lower() == "completed"
        and str(checkpoint.get("status") or "").strip().lower() == "done"
    )


def _closure_eligible(checkpoint: dict[str, Any], *, close_policy: str) -> bool:
    if not _is_completed_checkpoint(checkpoint):
        return False
    if close_policy == "checkpoint_done":
        return True
    if checkpoint.get("blockers"):
        return False
    next_step = str(checkpoint.get("next_step") or "").strip()
    if not next_step:
        return True
    return (
        checkpoint.get("next_step_scope") in {"none", "follow_up_task"}
        and checkpoint.get("next_step_scope_source") in {"explicit", "scope_review"}
    )


def _close_blockers(checkpoint: dict[str, Any], *, close_policy: str) -> list[str]:
    if close_policy == "checkpoint_done":
        return []
    blockers: list[str] = []
    if checkpoint.get("blockers"):
        blockers.append("checkpoint_blockers")
    next_step = str(checkpoint.get("next_step") or "").strip()
    if next_step:
        scope = str(checkpoint.get("next_step_scope") or "unknown")
        source = str(checkpoint.get("next_step_scope_source") or "absent")
        if source not in {"explicit", "scope_review"}:
            blockers.append("next_step_scope_not_explicit")
        elif scope == "same_artifact_remaining_work":
            blockers.append("same_artifact_remaining_work")
        elif scope == "operator_review":
            blockers.append("operator_review_required")
        elif scope not in {"none", "follow_up_task"}:
            blockers.append("next_step_scope_unknown")
    return blockers


def _recommendation(candidate: CompletedCheckpointArtifactCandidate) -> str:
    if candidate.closure_eligible:
        return "close"
    if candidate.blockers:
        return "review_blockers_before_closing"
    if "same_artifact_remaining_work" in candidate.close_blockers:
        return "continue_same_artifact"
    if "operator_review_required" in candidate.close_blockers:
        return "operator_review_before_closing"
    if "next_step_scope_not_explicit" in candidate.close_blockers:
        return "review_next_step_scope_before_closing"
    if candidate.next_step:
        return "review_next_step_before_closing"
    return "review"


def _add_review_group(response: ArtifactLifecycleReconcileResponse, candidate: CompletedCheckpointArtifactCandidate) -> None:
    if candidate.closure_eligible:
        group = "eligible_to_close"
    elif candidate.blockers:
        group = "blocked"
    elif candidate.recommendation == "continue_same_artifact":
        group = "same_artifact_remaining_work"
    elif candidate.recommendation == "operator_review_before_closing":
        group = "operator_review_required"
    elif candidate.recommendation == "review_next_step_scope_before_closing":
        group = "needs_next_step_scope"
    else:
        group = "needs_review"
    response.review_groups.setdefault(group, []).append(candidate.task_artifact_key)


def _apply_scope_review(checkpoint: dict[str, Any], scope_reviews: list[dict[str, Any]]) -> None:
    checkpoint_id = str(checkpoint.get("id") or "")
    matching = [
        review for review in scope_reviews
        if not str(review.get("target_checkpoint_id") or "").strip()
        or str(review.get("target_checkpoint_id") or "").strip() == checkpoint_id
    ]
    if not matching:
        return
    latest = matching[-1]
    checkpoint["next_step_scope"] = str(latest.get("next_step_scope") or "unknown")
    checkpoint["next_step_scope_source"] = "scope_review"


def _scope_review_decision(candidate: CompletedCheckpointArtifactCandidate) -> ArtifactLifecycleScopeReviewDecision | None:
    if candidate.recommendation != "review_next_step_scope_before_closing":
        return None
    suggested_scope = candidate.next_step_scope
    if suggested_scope not in _REVIEWABLE_NEXT_STEP_SCOPES:
        suggested_scope = "operator_review"
    reason = (
        "Review suggested by completed-checkpoint reconciliation. "
        f"Current inferred scope: {candidate.next_step_scope}; "
        f"next_step: {candidate.next_step[:220]}"
    ).strip()
    return ArtifactLifecycleScopeReviewDecision(
        task_id=candidate.task_id,
        checkpoint_change_id=candidate.checkpoint_change_id,
        next_step_scope=suggested_scope,
        reason=reason,
    )


def _attach_scope_review_batch_suggestion(response: ArtifactLifecycleReconcileResponse) -> None:
    decisions = [
        decision
        for candidate in response.candidates
        if (decision := _scope_review_decision(candidate)) is not None
    ][:50]
    if not decisions:
        response.suggested_scope_review_batch = None
        return
    response.suggested_scope_review_batch = ArtifactLifecycleScopeReviewBatchRequest(
        project=response.project,
        decisions=decisions,
        default_reason="Review completed checkpoint next_step scopes suggested by reconciliation.",
        acted_by="codex",
        source="reconcile_completed_checkpoints_suggestion",
    )

def _completed_but_open_repair_candidate(
    candidate: CompletedCheckpointArtifactCandidate,
) -> LifecycleAnomalyRepairCandidate:
    evidence_refs = [candidate.task_artifact_key]
    if candidate.checkpoint_change_id:
        evidence_refs.append(f"checkpoint:{candidate.checkpoint_change_id}")
    if candidate.linked_artifact_key:
        evidence_refs.append(candidate.linked_artifact_key)

    safe = bool(candidate.closure_eligible and not candidate.close_blockers)
    if safe:
        recommended_repair = "close_as_completed"
        recommended_close_status = "completed"
        reason = "A completed/done task checkpoint exists and strict close blockers are absent."
    elif candidate.recommendation == "continue_same_artifact":
        recommended_repair = "continue_same_artifact"
        recommended_close_status = ""
        reason = "The completion checkpoint still points to remaining work on the same artifact."
    elif candidate.recommendation == "review_next_step_scope_before_closing":
        recommended_repair = "review_next_step_scope"
        recommended_close_status = ""
        reason = "The completion checkpoint has a next_step whose scope was not explicitly reviewed."
    elif candidate.recommendation == "operator_review_before_closing":
        recommended_repair = "operator_review"
        recommended_close_status = ""
        reason = "The completion checkpoint explicitly requires operator review before closing."
    else:
        recommended_repair = "review_before_close"
        recommended_close_status = ""
        reason = "Completion evidence exists, but strict auto-repair evidence is insufficient."

    return LifecycleAnomalyRepairCandidate(
        project=candidate.project,
        task_id=candidate.task_id,
        task_artifact_key=candidate.task_artifact_key,
        current_status=candidate.task_status,
        safe_auto_repair=safe,
        recommended_repair=recommended_repair,
        recommended_close_status=recommended_close_status,
        reason=reason,
        evidence_refs=evidence_refs,
        checkpoint_change_id=candidate.checkpoint_change_id,
        checkpoint_summary=candidate.summary,
        next_step=candidate.next_step,
        next_step_scope=candidate.next_step_scope,
        next_step_scope_source=candidate.next_step_scope_source,
        close_blockers=list(candidate.close_blockers),
        linked_artifact_key=candidate.linked_artifact_key,
        linked_status=candidate.linked_status,
    )


async def list_completed_but_open_anomalies(
    *,
    project: str,
    close_policy: str = "strict",
    limit: int = 100,
) -> LifecycleAnomalyRepairResponse:
    reconciliation = await reconcile_completed_checkpoint_artifacts(
        project=project,
        close=False,
        close_policy=close_policy,
        limit=limit,
    )
    candidates = [_completed_but_open_repair_candidate(candidate) for candidate in reconciliation.candidates]
    safe_candidates = [candidate.task_artifact_key for candidate in candidates if candidate.safe_auto_repair]
    needs_operator_review = [candidate.task_artifact_key for candidate in candidates if not candidate.safe_auto_repair]
    return LifecycleAnomalyRepairResponse(
        project=reconciliation.project,
        scanned_tasks=reconciliation.scanned_tasks,
        candidate_count=len(candidates),
        safe_auto_repair_count=len(safe_candidates),
        review_required_count=len(needs_operator_review),
        candidates=candidates,
        safe_candidates=safe_candidates,
        needs_operator_review=needs_operator_review,
    )


async def reconcile_completed_checkpoint_artifacts(
    *,
    project: str,
    close: bool = False,
    close_policy: str = "strict",
    acted_by: str = "system",
    action_source: str = "completed_checkpoint_reconciliation",
    reason: str = "Completed task checkpoint indicates artifact lifecycle is stale.",
    limit: int = 100,
) -> ArtifactLifecycleReconcileResponse:
    tasks_store = get_project_tasks_store()
    artifact_service = get_unified_artifact_service()
    tasks = tasks_store.list_tasks(project=project, status="all", limit=limit)
    response = ArtifactLifecycleReconcileResponse(project=project, scanned_tasks=len(tasks))

    for task in tasks:
        task_status = str(task.get("status") or "").strip()
        if task_status not in _OPEN_TASK_STATUSES:
            continue
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            continue
        changes = tasks_store.list_changes(project=project, task_id=task_id, limit=500)
        checkpoints = [parsed for change in changes if (parsed := _parse_checkpoint_change(change)) is not None]
        scope_reviews = [parsed for change in changes if (parsed := _parse_scope_review_change(change)) is not None]
        completed = [checkpoint for checkpoint in checkpoints if _is_completed_checkpoint(checkpoint)]
        if not completed:
            continue

        latest = completed[-1]
        _apply_scope_review(latest, scope_reviews)
        explicit_scope = _normalize_next_step_scope(str(latest.get("next_step_scope") or ""))
        if explicit_scope != "unknown":
            latest["next_step_scope"] = explicit_scope
            current_source = str(latest.get("next_step_scope_source") or "")
            latest["next_step_scope_source"] = current_source if current_source == "scope_review" else "explicit"
        else:
            scope, source = _infer_next_step_scope(str(latest.get("next_step") or ""))
            latest["next_step_scope"] = scope
            latest["next_step_scope_source"] = source
        task_key = f"task:{project}:{task_id}"
        try:
            artifact = await artifact_service.get_artifact(task_key)
            linked_key = artifact.linked_artifact_key
            linked_status = artifact.linked_status
        except Exception:
            linked_key = None
            linked_status = None

        candidate = CompletedCheckpointArtifactCandidate(
            project=project,
            task_id=task_id,
            task_artifact_key=task_key,
            task_status=task_status,
            checkpoint_change_id=str(latest.get("id") or ""),
            checkpoint_timestamp=latest["timestamp"],
            checkpoint_stage=str(latest.get("stage") or ""),
            checkpoint_status=str(latest.get("status") or ""),
            summary=str(latest.get("summary") or ""),
            blockers=list(latest.get("blockers") or []),
            remaining_risk=list(latest.get("remaining_risk") or []),
            next_step=str(latest.get("next_step") or ""),
            next_step_scope=str(latest.get("next_step_scope") or "unknown"),
            next_step_scope_source=str(latest.get("next_step_scope_source") or "absent"),
            linked_artifact_key=linked_key,
            linked_status=linked_status,
            closure_eligible=_closure_eligible(latest, close_policy=close_policy),
            close_blockers=_close_blockers(latest, close_policy=close_policy),
        )
        candidate.recommendation = _recommendation(candidate)
        response.candidates.append(candidate)
        _add_review_group(response, candidate)

        if not close:
            continue
        if not candidate.closure_eligible:
            response.skipped_artifact_keys.append(task_key)
            continue
        try:
            resolved = await artifact_service.resolve_artifact(
                task_key,
                UnifiedArtifactResolveRequest(
                    acted_by=acted_by,
                    action_source=action_source,
                    reason=reason,
                ),
            )
            response.closed_artifact_keys.append(resolved.artifact_key)
            if resolved.linked_artifact_key and resolved.linked_status != "done":
                response.closed_artifact_keys.append(resolved.linked_artifact_key)
        except Exception as exc:
            response.errors.append(f"{task_key}: {exc}")

    _attach_scope_review_batch_suggestion(response)
    return response
