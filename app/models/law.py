from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


LAW_SCOPE_PATTERN = "^(project|family|domain|principle|meta)$"
LAW_STATUS_PATTERN = "^(observed|proposed|reviewed|user_confirmed|active|suppressed|superseded|archived)$"


class ProjectLawCreate(BaseModel):
    project: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    statement: str = Field(..., min_length=1, max_length=4000)
    rationale: str = Field("", max_length=4000)
    evidence: list[str] = Field(default_factory=list, max_length=64)
    agent_id: str = Field("llm", min_length=1, max_length=256)
    scope: str = Field("project", pattern=LAW_SCOPE_PATTERN)
    status: str = Field("proposed", pattern=LAW_STATUS_PATTERN)
    version: str = Field("1.0", max_length=64)
    supersedes: list[str] = Field(default_factory=list, max_length=64)
    supported_by: list[str] = Field(default_factory=list, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=64)
    topic_path: Optional[str] = Field(None, max_length=256)
    confirmed_by: Optional[str] = Field(None, max_length=256)
    confirmation_source: Optional[str] = Field(None, max_length=128)
    confirmed_at: Optional[datetime] = None


class ProjectLawUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=256)
    statement: Optional[str] = Field(None, min_length=1, max_length=4000)
    rationale: Optional[str] = Field(None, max_length=4000)
    evidence: Optional[list[str]] = Field(None, max_length=64)
    scope: Optional[str] = Field(None, pattern=LAW_SCOPE_PATTERN)
    version: Optional[str] = Field(None, max_length=64)
    supersedes: Optional[list[str]] = Field(None, max_length=64)
    supported_by: Optional[list[str]] = Field(None, max_length=128)
    tags: Optional[list[str]] = Field(None, max_length=64)
    topic_path: Optional[str] = Field(None, max_length=256)
    project: Optional[str] = Field(None, max_length=128)


class ProjectLawStatusUpdate(BaseModel):
    status: str = Field(..., pattern=LAW_STATUS_PATTERN)
    reason: str = Field("", max_length=1000)
    acted_by: str = Field("user", min_length=1, max_length=256)
    action_source: str = Field("inline_user_approval", max_length=128)


class ProjectLawConfirmRequest(BaseModel):
    confirmed_by: str = Field(..., min_length=1, max_length=256)
    confirmation_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("", max_length=1000)
    activate: bool = True


class ProjectLawCandidate(BaseModel):
    title: str
    statement: str
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)
    version: str = "1.0"
    scope: str = "project"
    project: Optional[str] = None
    topic_path: Optional[str] = None
    status: str = "proposed"
    proposed_at: datetime


class ProjectLawRecord(BaseModel):
    id: str
    project: Optional[str] = None
    scope: str
    status: str
    title: str
    statement: str
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)
    version: str = "1.0"
    supersedes: list[str] = Field(default_factory=list)
    supported_by: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    topic_path: Optional[str] = None
    source: str = ""
    created_at: datetime
    updated_at: datetime
    memory_id: str
    canonical_id: Optional[str] = None
    is_project_local: bool = True
    confirmed_by: Optional[str] = None
    confirmation_source: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    status_reason: str = ""
    last_status_action: Optional[str] = None
    last_status_acted_by: Optional[str] = None
    last_status_action_source: Optional[str] = None
    last_status_action_at: Optional[datetime] = None
    last_status_action_reason: Optional[str] = None
    candidate_revision: Optional[ProjectLawCandidate] = None


class ProjectLawListResponse(BaseModel):
    total: int
    items: list[ProjectLawRecord]


class ProjectLawImportRequest(BaseModel):
    project: str = Field(..., min_length=1, max_length=128)
    path: str = Field(..., min_length=1, max_length=1024)
    agent_id: str = Field("system", min_length=1, max_length=256)
    confirmed_by: str = Field(..., min_length=1, max_length=256)
    confirmation_source: str = Field("inline_user_approval", max_length=128)
    reason: str = Field("Imported from project law markdown", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=64)


class ProjectLawImportResponse(BaseModel):
    project: str
    source_path: str
    parsed: int
    created: int
    skipped_existing: int
    staged_candidate_revision: int
    created_ids: list[str] = Field(default_factory=list)
    staged_ids: list[str] = Field(default_factory=list)
