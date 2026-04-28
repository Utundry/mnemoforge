from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


CHECKPOINT_DRAFT_STATUS_PATTERN = "^(drafted|revised|approved|rejected|expired)$"


class CheckpointDraftRecord(BaseModel):
    draft_id: str
    version: int
    status: str = Field(..., pattern=CHECKPOINT_DRAFT_STATUS_PATTERN)
    project: str
    task_id: str
    work_id: str = ""
    agent_id: str = "codex"
    session_id: str = ""
    preview: str
    record_task_checkpoint_args: dict[str, Any]
    validation_report: dict[str, Any] = Field(default_factory=dict)
    source_span_ids: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    created_by: str = "codex"
    approved_by: str = ""
    rejected_by: str = ""
    rejection_reason: str = ""
    saved_change_id: str = ""
    created_at: datetime
    updated_at: datetime
    approved_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
