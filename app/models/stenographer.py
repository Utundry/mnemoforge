from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


WORK_SESSION_STATUS_PATTERN = "^(active|parked|completed|blocked|failed|interrupted|cancelled)$"
WORK_SESSION_TERMINAL_STATUSES = {"completed", "blocked", "failed", "interrupted", "cancelled"}
STENOGRAPHER_KIND_PATTERN = "^(fact|decision|verification|risk|blocker|next_step|checkpoint_hint|handoff_hint|diagnostic|changed_files|rule_project_candidate|rule_canonical_candidate|rule_revision_hint|rule_merge_hint)$"


class WorkSessionRecord(BaseModel):
    work_id: str
    project: str
    task_id: str
    agent_id: str
    session_id: str
    role: str = "worker"
    status: str = Field("active", pattern=WORK_SESSION_STATUS_PATTERN)
    parent_work_id: str = ""
    parent_task_id: str = ""
    spawn_reason: str = ""
    return_condition: str = ""
    scope: list[str] = Field(default_factory=list)
    summary: str = ""
    result: str = ""
    created_at: datetime
    updated_at: datetime
    ended_at: Optional[datetime] = None


class StenographerSpanRecord(BaseModel):
    span_id: str
    project: str
    task_id: str
    work_id: str
    agent_id: str
    session_id: str
    kind: str = Field(..., pattern=STENOGRAPHER_KIND_PATTERN)
    source: str = ""
    content: str
    content_hash: str
    status: str = "active"
    redaction_report: list[str] = Field(default_factory=list)
    excluded_from_learning: bool = True
    created_at: datetime


class WorkSessionState(BaseModel):
    project: str = ""
    task_id: str = ""
    agent_id: str
    session_id: str
    state: str
    active_work: Optional[WorkSessionRecord] = None
    parked_stack: list[WorkSessionRecord] = Field(default_factory=list)
    next_valid_tools: list[str] = Field(default_factory=list)
    protocol_violations: list[str] = Field(default_factory=list)
    closeout_required: bool = False
    closeout_ready: bool = False
    closeout_missing: list[str] = Field(default_factory=list)
    closeout_evidence: dict[str, list[str]] = Field(default_factory=dict)
