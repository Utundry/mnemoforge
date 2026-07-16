from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CompletedCheckpointArtifactCandidate(BaseModel):
    project: str
    task_id: str
    task_artifact_key: str
    task_status: str
    checkpoint_change_id: str
    checkpoint_timestamp: datetime
    checkpoint_stage: str
    checkpoint_status: str
    summary: str = ""
    blockers: list[str] = Field(default_factory=list)
    remaining_risk: list[str] = Field(default_factory=list)
    next_step: str = ""
    next_step_scope: str = "unknown"
    next_step_scope_source: str = "absent"
    linked_artifact_key: str | None = None
    linked_status: str | None = None
    closure_eligible: bool = False
    close_blockers: list[str] = Field(default_factory=list)
    recommendation: str = ""


class ArtifactLifecycleReconcileRequest(BaseModel):
    project: str = Field("mnemoforge", min_length=1, max_length=128)
    close: bool = Field(False, description="When true, close eligible artifacts; otherwise report only.")
    close_policy: Literal["strict", "checkpoint_done"] = "strict"
    acted_by: str = Field("system", min_length=1, max_length=256)
    action_source: str = Field("completed_checkpoint_reconciliation", max_length=128)
    reason: str = Field("Completed task checkpoint indicates artifact lifecycle is stale.", max_length=500)
    limit: int = Field(100, ge=1, le=500)

class LifecycleAnomalyRepairCandidate(BaseModel):
    anomaly_type: Literal["completed_but_open"] = "completed_but_open"
    project: str
    task_id: str
    task_artifact_key: str
    current_status: str
    safe_auto_repair: bool = False
    recommended_repair: str
    recommended_close_status: str = ""
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    checkpoint_change_id: str = ""
    checkpoint_summary: str = ""
    next_step: str = ""
    next_step_scope: str = "unknown"
    next_step_scope_source: str = "absent"
    close_blockers: list[str] = Field(default_factory=list)
    linked_artifact_key: str | None = None
    linked_status: str | None = None


class LifecycleAnomalyRepairResponse(BaseModel):
    project: str
    anomaly_type: Literal["completed_but_open"] = "completed_but_open"
    scanned_tasks: int = 0
    candidate_count: int = 0
    safe_auto_repair_count: int = 0
    review_required_count: int = 0
    candidates: list[LifecycleAnomalyRepairCandidate] = Field(default_factory=list)
    safe_candidates: list[str] = Field(default_factory=list)
    needs_operator_review: list[str] = Field(default_factory=list)
    source_route: str = "reconcile_completed_checkpoint_artifacts"


class ArtifactLifecycleScopeReviewRequest(BaseModel):
    project: str = Field("mnemoforge", min_length=1, max_length=128)
    task_id: str = Field(..., min_length=1, max_length=256)
    checkpoint_change_id: str = Field("", max_length=128)
    next_step_scope: Literal["none", "follow_up_task", "same_artifact_remaining_work", "operator_review"] = "operator_review"
    reason: str = Field("Review completed checkpoint next_step scope.", max_length=500)
    acted_by: str = Field("user", min_length=1, max_length=256)
    source: str = Field("artifact_lifecycle_scope_review", max_length=128)


class ArtifactLifecycleScopeReviewDecision(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=256)
    checkpoint_change_id: str = Field("", max_length=128)
    next_step_scope: Literal["none", "follow_up_task", "same_artifact_remaining_work", "operator_review"]
    reason: str = Field("", max_length=500)


class ArtifactLifecycleScopeReviewBatchRequest(BaseModel):
    project: str = Field("mnemoforge", min_length=1, max_length=128)
    decisions: list[ArtifactLifecycleScopeReviewDecision] = Field(..., min_length=1, max_length=50)
    default_reason: str = Field("Batch review completed checkpoint next_step scopes.", max_length=500)
    acted_by: str = Field("user", min_length=1, max_length=256)
    source: str = Field("artifact_lifecycle_scope_review_batch", max_length=128)


class ArtifactLifecycleScopeReviewResponse(BaseModel):
    project: str
    task_id: str
    checkpoint_change_id: str = ""
    next_step_scope: str
    saved_change_id: str
    content: str


class ArtifactLifecycleScopeReviewBatchResponse(BaseModel):
    project: str
    saved_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    saved: list[ArtifactLifecycleScopeReviewResponse] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    errors: list[dict[str, str]] = Field(default_factory=list)


class ArtifactLifecycleReconcileResponse(BaseModel):
    project: str
    scanned_tasks: int = 0
    candidates: list[CompletedCheckpointArtifactCandidate] = Field(default_factory=list)
    review_groups: dict[str, list[str]] = Field(default_factory=dict)
    suggested_scope_review_batch: ArtifactLifecycleScopeReviewBatchRequest | None = None
    closed_artifact_keys: list[str] = Field(default_factory=list)
    skipped_artifact_keys: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
