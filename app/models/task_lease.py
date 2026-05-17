from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


TASK_LEASE_STATUS_PATTERN = "^(active|released|expired|transferred)$"
TASK_LEASE_TERMINAL_STATUSES = {"released", "expired", "transferred"}


class TaskLeaseRecord(BaseModel):
    lease_id: str
    project: str
    task_id: str
    owner_agent: str
    session_id: str = ""
    status: str = Field("active", pattern=TASK_LEASE_STATUS_PATTERN)
    claimed_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: Optional[datetime] = None
    release_reason: str = ""
    lease_ttl_seconds: int
    previous_lease_id: str = ""
    work_token_hash: str = ""
    work_token_preview: str = ""


class TaskLeaseClaimResult(BaseModel):
    status: str
    lease: TaskLeaseRecord
    previous_claim_expired: bool = False
    previous_lease: Optional[TaskLeaseRecord] = None
    work_token: str = ""

