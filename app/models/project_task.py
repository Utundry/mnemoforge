from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


TASK_STATUS_PATTERN = "^(planning|active|done|paused|archived)$"
TASK_CHANGE_TYPE_PATTERN = "^(note|decision|implementation|status_change|task_created)$"


class ProjectTaskCreate(BaseModel):
    task_id: Optional[str] = Field(None, min_length=1, max_length=256)
    project: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field("", max_length=10000)
    agent_id: str = Field("user", min_length=1, max_length=256)
    status: str = Field("planning", pattern=TASK_STATUS_PATTERN)
    source: str = Field("project-task", max_length=128)
    tags: list[str] = Field(default_factory=list)
    topic_path: Optional[str] = Field(None, max_length=256)
    linked_improvement_id: Optional[str] = Field(None, max_length=64)


class ProjectTaskReopenRequest(BaseModel):
    project: Optional[str] = Field(None, min_length=1, max_length=128)
    status: str = Field("active", pattern="^(planning|active)$")
    reason: str = Field("reopen_task", max_length=500)
    acted_by: str = Field("user", min_length=1, max_length=256)
    source: str = Field("project-task", max_length=128)


class ProjectTaskChangeCreate(BaseModel):
    project: Optional[str] = Field(None, max_length=128)
    change_type: str = Field("note", pattern=TASK_CHANGE_TYPE_PATTERN)
    content: str = Field(..., min_length=1, max_length=10000)
    why: str = Field("", max_length=2000)
    agent_id: str = Field("user", min_length=1, max_length=256)
    source: str = Field("project-task", max_length=128)
    tags: list[str] = Field(default_factory=list)


class ProjectTaskChangeRecord(BaseModel):
    id: UUID
    task_id: str
    project: str
    change_type: str
    content: str
    why: str
    agent_id: str
    source: str
    tags: list[str]
    timestamp: datetime


class ProjectTaskRecord(BaseModel):
    id: UUID
    task_id: str
    project: str
    title: str
    description: str
    agent_id: str
    status: str
    source: str
    tags: list[str]
    topic_path: Optional[str] = None
    linked_improvement_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    task_capture_pending_count: int = 0
    task_capture_promoted_count: int = 0
    task_statement_incomplete: bool = False
    changes: list[ProjectTaskChangeRecord] = Field(default_factory=list)


class ProjectTaskListResponse(BaseModel):
    total: int
    items: list[ProjectTaskRecord]


class ProjectTaskBackfillResponse(BaseModel):
    project: Optional[str] = None
    scanned: int
    created: int
    skipped_existing: int
    failed: int = 0
    failed_task_ids: list[str] = Field(default_factory=list)


class TaskStatementTimelineItem(BaseModel):
    kind: str
    timestamp: datetime
    title: str
    detail: str = ""
    rationale: str = ""
    source_artifact_id: str = ""
    inferred: bool = False


class TaskStatementFieldEvolutionItem(BaseModel):
    field: str
    value: str
    timestamp: datetime
    rationale: str = ""
    source_artifact_id: str = ""
    source_kind: str = ""


class TaskStatementActionItem(BaseModel):
    priority: str
    action: str
    rationale: str = ""
    source_kind: str = ""


class TaskStatementCurrentView(BaseModel):
    objective: str
    scope_summary: str
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    definition_of_done: list[str] = Field(default_factory=list)
    chosen_decisions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    deferred_work: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)


class TaskStatementDiffView(BaseModel):
    original_objective: str
    current_objective: str
    new_decisions: list[str] = Field(default_factory=list)
    changed_constraints: list[str] = Field(default_factory=list)
    newly_deferred: list[str] = Field(default_factory=list)
    execution_changes: list[str] = Field(default_factory=list)
    framing_evolution: list[TaskStatementFieldEvolutionItem] = Field(default_factory=list)
    unresolved_ambiguities: list[str] = Field(default_factory=list)
    changed: bool = False


class TaskStatementQualityView(BaseModel):
    capture_quality: str
    missing_artifacts: list[str] = Field(default_factory=list)
    grounded_by: list[str] = Field(default_factory=list)


class TaskCaptureCandidateRecord(BaseModel):
    kind: str
    content: str
    source: str
    confidence: float = 0.0
    rationale: str = ""
    artifact_id: str = ""
    artifact_type: str = "task_capture_candidate"
    reused_existing: bool = False


class TaskCaptureReviewRecord(TaskCaptureCandidateRecord):
    status: str = ""
    updated_at: Optional[datetime] = None
    status_updated_by: str = ""
    status_update_source: str = ""
    status_update_reason: str = ""
    last_review_action: str = ""


class TaskStatementCaptureReviewView(BaseModel):
    pending_count: int = 0
    promoted_count: int = 0
    pending_candidates: list[TaskCaptureReviewRecord] = Field(default_factory=list)
    promoted_candidates: list[TaskCaptureReviewRecord] = Field(default_factory=list)


class TaskStatementProjectionResponse(BaseModel):
    task: ProjectTaskRecord
    current: TaskStatementCurrentView
    timeline: list[TaskStatementTimelineItem] = Field(default_factory=list)
    diff: TaskStatementDiffView
    quality: TaskStatementQualityView
    capture_review: TaskStatementCaptureReviewView = Field(default_factory=TaskStatementCaptureReviewView)
    next_actions: list[TaskStatementActionItem] = Field(default_factory=list)


class TaskCaptureCompletionResponse(BaseModel):
    statement: TaskStatementProjectionResponse
    missing_before: list[str] = Field(default_factory=list)
    missing_after: list[str] = Field(default_factory=list)
    local_generation_used: bool = False
    local_model: str = ""
    local_generation_error: str = ""
    persisted_count: int = 0
    reused_count: int = 0
    candidates: list[TaskCaptureCandidateRecord] = Field(default_factory=list)


class TaskCaptureCandidateListResponse(BaseModel):
    project: str
    task_id: str
    found: int
    candidates: list[TaskCaptureCandidateRecord] = Field(default_factory=list)


class TaskCapturePromoteRequest(BaseModel):
    artifact_ids: list[str] = Field(..., min_length=1, max_length=20)
    acted_by: str = Field("codex", min_length=1, max_length=256)
    review_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("promote_task_capture_candidate", max_length=500)


class TaskCapturePromoteResponse(BaseModel):
    project: str
    task_id: str
    promoted_count: int = 0
    archived_count: int = 0
    skipped_count: int = 0
    promoted_artifact_ids: list[str] = Field(default_factory=list)
    skipped_artifact_ids: list[str] = Field(default_factory=list)


class TaskCaptureRejectRequest(BaseModel):
    artifact_ids: list[str] = Field(..., min_length=1, max_length=20)
    acted_by: str = Field("codex", min_length=1, max_length=256)
    review_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("reject_task_capture_candidate", max_length=500)


class TaskCaptureRejectResponse(BaseModel):
    project: str
    task_id: str
    rejected_count: int = 0
    archived_count: int = 0
    skipped_count: int = 0
    rejected_artifact_ids: list[str] = Field(default_factory=list)
    skipped_artifact_ids: list[str] = Field(default_factory=list)


# Additional task capture artifact models for MVP completion


class DecisionCandidateRecord(BaseModel):
    """Кандидат на решение для задачи."""
    artifact_id: str
    task_id: str
    project: str
    description: str
    rationale: str
    alternatives: list[str] = Field(default_factory=list)
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    status: str = "candidate"  # candidate, chosen, rejected
    created_at: datetime
    chosen_at: Optional[datetime] = None
    chosen_by: str = ""


class ChosenDecisionRecord(BaseModel):
    """Выбранное решение для задачи."""
    artifact_id: str
    task_id: str
    project: str
    description: str
    rationale: str
    rejected_alternatives: list[str] = Field(default_factory=list)
    impact: str = ""
    created_at: datetime
    chosen_by: str = ""


class CodeLinkRecord(BaseModel):
    """Ссылка на код, связанный с задачей."""
    artifact_id: str
    task_id: str
    project: str
    file_path: str
    line_range: str = ""
    description: str
    change_type: str = "implementation"  # implementation, fix, refactor
    created_at: datetime
    created_by: str = ""


class RemainingRiskRecord(BaseModel):
    """Оставшийся риск после завершения задачи."""
    artifact_id: str
    task_id: str
    project: str
    description: str
    likelihood: str = "medium"  # low, medium, high
    impact: str = "medium"  # low, medium, high
    mitigation: str = ""
    owner: Optional[str] = None
    created_at: datetime
    created_by: str = ""
