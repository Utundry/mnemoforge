from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


COORDINATION_MESSAGE_TYPE_PATTERN = "^(question|request_action|response|status_update|handoff|note)$"
COORDINATION_STATUS_PATTERN = "^(new|acknowledged|in_progress|answered|closed)$"
COORDINATION_PRIORITY_PATTERN = "^(low|normal|high|urgent)$"
COORDINATION_MAILBOX_PATTERN = "^(inbox|outbox|thread)$"


class CoordinationMessageCreate(BaseModel):
    project: str = Field(..., min_length=1, max_length=128)
    from_agent: str = Field(..., min_length=1, max_length=128)
    to_agent: str = Field(..., min_length=1, max_length=128)
    message_type: str = Field("question", pattern=COORDINATION_MESSAGE_TYPE_PATTERN)
    content: str = Field(..., min_length=1, max_length=10000)
    thread_id: Optional[str] = Field(None, min_length=1, max_length=128)
    response_to_message_id: Optional[str] = Field(None, min_length=1, max_length=128)
    requested_action: Optional[str] = Field(None, max_length=512)
    priority: str = Field("normal", pattern=COORDINATION_PRIORITY_PATTERN)
    source: str = Field("coordination", max_length=128)
    tags: list[str] = Field(default_factory=list)


class CoordinationPickupRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    project: Optional[str] = Field(None, min_length=1, max_length=128)
    limit: int = Field(10, ge=1, le=100)


class CoordinationStatusUpdate(BaseModel):
    status: str = Field(..., pattern=COORDINATION_STATUS_PATTERN)
    acted_by: str = Field(..., min_length=1, max_length=128)
    action_source: str = Field("coordination_api", max_length=128)
    reason: Optional[str] = Field(None, max_length=1000)


class CoordinationMessageRecord(BaseModel):
    memory_id: str
    project: str
    thread_id: str
    from_agent: str
    to_agent: str
    message_type: str
    content: str
    status: str
    priority: str
    requested_action: str
    response_to_message_id: str
    source: str
    tags: list[str]
    timestamp: datetime
    last_status_action: Optional[str] = None
    last_status_acted_by: Optional[str] = None
    last_status_action_source: Optional[str] = None
    last_status_action_at: Optional[datetime] = None
    last_status_action_reason: Optional[str] = None


class CoordinationListResponse(BaseModel):
    total: int
    items: list[CoordinationMessageRecord]
