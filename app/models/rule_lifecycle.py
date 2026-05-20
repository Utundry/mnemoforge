from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


RULE_MARKER_KINDS = {
    "rule_project_candidate",
    "rule_canonical_candidate",
    "rule_revision_hint",
    "rule_merge_hint",
}

RULE_CANDIDATE_STATUS_PATTERN = "^(candidate|needs_clarification|trial|revision_pending|rejected|suppressed)$"
RULE_SCOPE_PATTERN = "^(project|canonical_candidate)$"
RULE_CANDIDATE_REVIEW_ACTION_PATTERN = "^(reject|suppress|needs_clarification|reopen)$"


class RuleCandidateRecord(BaseModel):
    candidate_id: str
    project: str
    scope: str = Field(..., pattern=RULE_SCOPE_PATTERN)
    topic_path: str = ""
    marker_kind: str
    statement: str
    rationale: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    source_task_id: str = ""
    source_session_id: str = ""
    source_span_id: str
    source_work_id: str = ""
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    promotion_hint: str = ""
    related_rule_hint: Optional[str] = None
    status: str = Field("candidate", pattern=RULE_CANDIDATE_STATUS_PATTERN)
    last_review_action: str = ""
    last_review_reason: str = ""
    last_review_acted_by: str = ""
    last_review_source: str = ""
    last_review_at: Optional[datetime] = None
    promoted_law_id: str = ""
    promoted_at: Optional[datetime] = None
    revised_law_id: str = ""
    revised_at: Optional[datetime] = None
    trial_started_at: Optional[datetime] = None
    trial_review_after: Optional[datetime] = None
    trial_expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class RuleCandidateProjectionReport(BaseModel):
    scanned_spans: int
    created_candidates: int
    skipped_spans: int
    errors: list[dict] = Field(default_factory=list)
    last_processed_timestamp: float
    candidates: list[RuleCandidateRecord] = Field(default_factory=list)


class RuleCandidateProjectionRequest(BaseModel):
    project: Optional[str] = Field(None, max_length=128)
    limit: int = Field(500, ge=1, le=2000)


class RuleCandidateCreateRequest(BaseModel):
    project: str = Field(..., min_length=1, max_length=128)
    statement: str = Field(..., min_length=1, max_length=1200)
    rationale: str = Field("", max_length=2000)
    title: str = Field("", max_length=256)
    scope: str = Field("project", pattern=RULE_SCOPE_PATTERN)
    topic_path: str = Field("", max_length=256)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    source_task_id: str = Field("", max_length=256)
    source_session_id: str = Field("", max_length=256)
    source_span_id: str = Field("", max_length=256)
    source_work_id: str = Field("", max_length=256)
    confidence: float = Field(0.75, ge=0.0, le=1.0)
    promotion_hint: str = Field("", max_length=1000)
    related_rule_hint: Optional[str] = Field(None, max_length=512)
    status: str = Field("trial", pattern=RULE_CANDIDATE_STATUS_PATTERN)
    review_after_days: int = Field(7, ge=0, le=365)
    trial_days: int = Field(30, ge=1, le=3650)
    acted_by: str = Field("codex", max_length=256)
    source: str = Field("mcp_project_rules", max_length=128)


class RuleCandidateListResponse(BaseModel):
    total: int
    items: list[RuleCandidateRecord]


class RuleCandidateReviewRequest(BaseModel):
    project: Optional[str] = Field(None, max_length=128)
    status: Optional[str] = Field("candidate", pattern=RULE_CANDIDATE_STATUS_PATTERN)
    source_task_id: Optional[str] = Field(None, max_length=256)
    review_due: bool = False
    limit: int = Field(100, ge=1, le=500)
    max_matches: int = Field(5, ge=0, le=20)


class RuleCandidateTrialExpireRequest(BaseModel):
    project: Optional[str] = Field(None, max_length=128)
    limit: int = Field(100, ge=1, le=500)
    reason: str = Field("Trial rule candidate expired without enough evidence.", max_length=1000)
    acted_by: str = Field("system", max_length=256)
    source: str = Field("rule_candidate_trial_expiry", max_length=128)


class RuleCandidateTrialExpireResponse(BaseModel):
    expired_count: int
    candidates: list[RuleCandidateRecord] = Field(default_factory=list)


class RuleCandidateSimilarityMatch(BaseModel):
    match_type: str
    id: str
    title: str = ""
    statement: str
    status: str = ""
    scope: str = ""
    topic_path: str = ""
    score: float = Field(0.0, ge=0.0, le=1.0)
    reason: str = ""


class RuleCandidateReviewItem(BaseModel):
    candidate: RuleCandidateRecord
    matching_laws: list[RuleCandidateSimilarityMatch] = Field(default_factory=list)
    matching_candidates: list[RuleCandidateSimilarityMatch] = Field(default_factory=list)
    recommendation: str
    rationale: str


class RuleCandidateReviewPacket(BaseModel):
    project: Optional[str] = None
    total_candidates: int
    items: list[RuleCandidateReviewItem]
    risk_controls: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class RuleCandidateReviewActionRequest(BaseModel):
    action: str = Field(..., pattern=RULE_CANDIDATE_REVIEW_ACTION_PATTERN)
    reason: str = Field(..., min_length=1, max_length=1000)
    acted_by: str = Field("user", min_length=1, max_length=256)
    source: str = Field("rule_candidate_operator_review", max_length=128)


class RuleCandidateReviewActionResponse(BaseModel):
    candidate: RuleCandidateRecord
    previous_status: str
    new_status: str
    action: str


class RuleCandidatePromoteRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=256)
    target_scope: Optional[str] = Field(None, pattern="^(project|family|domain|principle|meta)$")
    status: str = Field("proposed", pattern="^(proposed|user_confirmed|active)$")
    reason: str = Field(..., min_length=1, max_length=1000)
    acted_by: str = Field("user", min_length=1, max_length=256)
    source: str = Field("rule_candidate_promotion", max_length=128)
    confirmed_by: Optional[str] = Field(None, max_length=256)
    confirmation_source: str = Field("rule_candidate_promotion", max_length=128)


class RuleCandidatePromoteResponse(BaseModel):
    candidate: RuleCandidateRecord
    law: object
    previous_status: str
    new_status: str


class RuleCandidateReviseLawRequest(BaseModel):
    law_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=1000)
    acted_by: str = Field("user", min_length=1, max_length=256)
    source: str = Field("rule_candidate_law_revision", max_length=128)
    title: Optional[str] = Field(None, min_length=1, max_length=256)
    statement: Optional[str] = Field(None, min_length=1, max_length=4000)
    rationale: Optional[str] = Field(None, max_length=4000)
    evidence: Optional[list[str]] = Field(None, max_length=64)


class RuleCandidateReviseLawResponse(BaseModel):
    candidate: RuleCandidateRecord
    law: object
    previous_status: str
    new_status: str
