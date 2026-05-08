from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from app.models.enums import MemoryType


IMPROVEMENT_STAGES: tuple[str, ...] = ("proposal", "beta_test", "experimental", "stable", "deprecated")
IMPROVEMENT_VERDICTS: tuple[str, ...] = ("effective", "ineffective")


# Decay rate defaults by category — relevance decay, not validity.
# Evergreen: content that stays true regardless of how long the project sleeps.
# Fast: situational content that expires quickly even within an active project.
_CATEGORY_DECAY_DEFAULTS: dict[str, float] = {
    # Evergreen — never decays
    "procedure": 0.0, "architecture": 0.0, "policy": 0.0,
    "runbook": 0.0, "howto": 0.0, "decision": 0.0, "rule": 0.0,
    # Stable facts — slow decay
    "fact": 0.5, "reference": 0.5, "documentation": 0.5,
    # Observations — standard decay
    "general": 1.0, "qa": 1.0, "context": 1.0,
    # Situational — fast decay
    "status": 3.0, "incident": 3.0, "ops": 3.0, "price": 3.0,
    "availability": 3.0, "news": 3.0,
}


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
    agent_id: str = Field(..., min_length=1, max_length=256)
    memory_type: MemoryType = MemoryType.fact
    category: str = Field("general", max_length=128)
    importance_score: float = Field(0.5, ge=0.0, le=1.0)
    source: str = Field("conversation", max_length=128)
    tags: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None
    status: Optional[str] = Field(None, max_length=64, description="Optional lifecycle status for governed knowledge entities.")
    meta: dict[str, Any] = Field(default_factory=dict, description="Structured metadata for higher-level knowledge entities.")
    decay_rate: Optional[float] = Field(
        None, ge=0.0, le=10.0,
        description=(
            "Controls how fast this memory loses relevance over time. "
            "If omitted, auto-selected by category: procedure/architecture/policy/rule → 0.0, "
            "fact/reference → 0.5, status/incident/ops → 3.0, others → 1.0."
        ),
    )

    @property
    def effective_decay_rate(self) -> float:
        if self.decay_rate is not None:
            return self.decay_rate
        return _CATEGORY_DECAY_DEFAULTS.get(self.category, 1.0)
    pinned: bool = Field(False, description="Pin this memory — prevents importance decay and auto-deletion.")
    related_ids: list[str] = Field(default_factory=list, description="IDs of related memories (dependency graph).")
    project: Optional[str] = Field(None, max_length=128, description="Project this memory belongs to. Used for project-scoped decay gate.")
    expires_at: Optional[datetime] = Field(None, description="Hard expiry for time-sensitive content (status, price, incident). After this datetime the memory is heavily penalised in scoring.")
    # Knowledge tree fields
    topic_path: Optional[str] = Field(None, max_length=256, description="Hierarchical topic path, e.g. 'infra/nginx/reverse-proxy' or 'python/fastapi/errors'. Separate from category (which is a type/class).")
    scope: str = Field("project", description="Abstraction level: config|project|family|domain|principle|meta. Source knowledge usually lives at config/project/family, canonicals at domain/principle/meta.")
    supports: list[str] = Field(default_factory=list, description="For canonical memories: IDs of supporting lower-level memories or canonicals.")
    canonical_id: Optional[str] = Field(None, description="For leaf memories: ID of the canonical (L3+) memory this leaf has been crystallised into.")


class MemoryUpdate(BaseModel):
    content: Optional[str] = Field(None, min_length=1, max_length=10000)
    memory_type: Optional[MemoryType] = None
    category: Optional[str] = Field(None, max_length=128)
    importance_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    source: Optional[str] = Field(None, max_length=128)
    tags: Optional[list[str]] = None
    session_id: Optional[str] = None
    status: Optional[str] = Field(None, max_length=64)
    meta: Optional[dict[str, Any]] = None
    decay_rate: Optional[float] = Field(None, ge=0.0, le=10.0)
    pinned: Optional[bool] = None
    project: Optional[str] = Field(None, max_length=128)
    expires_at: Optional[datetime] = None
    topic_path: Optional[str] = Field(None, max_length=256)
    scope: Optional[str] = None
    canonical_id: Optional[str] = None


class MemoryRecord(BaseModel):
    id: UUID
    content: str
    agent_id: str
    memory_type: MemoryType
    category: str
    importance_score: float
    timestamp: datetime
    source: str
    tags: list[str]
    access_count: int
    session_id: Optional[str]
    status: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)
    decay_rate: float = 1.0
    pinned: bool = False
    last_access_ts: Optional[datetime] = None
    last_decay_ts: Optional[datetime] = None
    related_ids: list[str] = Field(default_factory=list)
    project: Optional[str] = None
    expires_at: Optional[datetime] = None
    topic_path: Optional[str] = None
    scope: str = "project"
    supports: list[str] = Field(default_factory=list)
    canonical_id: Optional[str] = None

    model_config = {"from_attributes": True}


class ImprovementCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1, max_length=10000)
    project: str = Field("mnemoforge", max_length=128)
    agent_id: str = Field("llm", max_length=256)
    importance_score: float = Field(
        0.7,
        ge=0.0,
        le=10.0,
        description="Importance on 0..1 scale; values >1 and <=10 are accepted as 1..10 shorthand and normalized.",
    )
    tags: list[str] = Field(default_factory=list)
    stage: str = Field("proposal", pattern="^(proposal|beta_test|experimental|stable|deprecated)$")
    verdict: str | None = Field(None, pattern="^(effective|ineffective)$")

    @field_validator("importance_score", mode="before")
    @classmethod
    def normalize_importance_score(cls, value: Any) -> Any:
        if value is None or value == "":
            return value
        score = float(value)
        if score > 1.0 and score <= 10.0:
            return score / 10.0
        return score


class ImprovementRecord(BaseModel):
    id: UUID
    title: str
    description: str
    project: str
    agent_id: str
    importance_score: float
    timestamp: datetime
    status: str
    tags: list[str]
    stage: str = "proposal"
    verdict: Optional[str] = None
    resolved_at: Optional[datetime] = None
    report_count: int = 1
    report_history: list[dict] = []
    last_status_action: Optional[str] = None
    last_status_acted_by: Optional[str] = None
    last_status_action_source: Optional[str] = None
    last_status_action_at: Optional[datetime] = None
    last_status_action_reason: Optional[str] = None
    last_quality_review_by: Optional[str] = None
    last_quality_review_source: Optional[str] = None
    last_quality_review_at: Optional[datetime] = None
    last_quality_review_reason: Optional[str] = None


class ImprovementReviewRequest(BaseModel):
    stage: str | None = Field(None, pattern="^(proposal|beta_test|experimental|stable|deprecated)$")
    verdict: str | None = Field(None, pattern="^(effective|ineffective)$")
    reviewed_by: str = Field("user", min_length=1, max_length=256)
    review_source: str = Field("manual_review", max_length=128)
    reason: str = Field("", max_length=1000)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    agent_id: Optional[str] = None
    memory_type: Optional[MemoryType] = None
    category: Optional[str] = None
    topic_prefix: Optional[str] = Field(None, max_length=256, description="Optional topic_path prefix filter")
    limit: int = Field(10, ge=1, le=100)
    min_score: float = Field(0.0, ge=0.0, le=1.0)
    since_minutes: Optional[int] = Field(None, ge=1, description="Only return memories added within last N minutes")
    # Context hints for enriched scoring — memories whose tags match these get a score boost
    context_project: Optional[str] = Field(None, max_length=128, description="Boost memories tagged project:<this>")
    context_file: Optional[str] = Field(None, max_length=512, description="Boost memories tagged file:<this>")
    context_task_type: Optional[str] = Field(None, max_length=128, description="Boost memories tagged task_type:<this>")


class SearchResult(BaseModel):
    memory: MemoryRecord
    score: float
    similarity: float


class CleanupRequest(BaseModel):
    agent_id: Optional[str] = None
    min_importance: float = Field(0.2, ge=0.0, le=1.0)
    max_age_days: int = Field(30, ge=1)


class CleanupResponse(BaseModel):
    deleted_count: int


class BatchCreateRequest(BaseModel):
    memories: list[MemoryCreate] = Field(..., min_length=1, max_length=100)


class BatchCreateResponse(BaseModel):
    created_ids: list[UUID]
    failed_count: int


class ContextRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    agent_id: Optional[str] = None
    memory_type: Optional[MemoryType] = None
    category: Optional[str] = None
    topic_prefix: Optional[str] = Field(None, max_length=256, description="Optional topic_path prefix filter")
    limit: int = Field(10, ge=1, le=50)
    min_score: float = Field(0.0, ge=0.0, le=1.0)
    since_minutes: Optional[int] = Field(None, ge=1)
    max_tokens: int = Field(2000, ge=100, le=10000, description="Token budget for context output")
    format: str = Field("markdown", pattern="^(text|markdown)$")
    context_project: Optional[str] = Field(None, max_length=128)
    context_file: Optional[str] = Field(None, max_length=512)
    context_task_type: Optional[str] = Field(None, max_length=128)
    session_id: Optional[str] = Field(
        None,
        max_length=128,
        description="Optional session/episode id to link /context use-events with later outcome feedback.",
    )


class PendingHint(BaseModel):
    """Lightweight stub for a scout best-practice candidate awaiting user review."""
    id: str
    title: str
    domain: str


class ContextBundleResponse(BaseModel):
    context: str
    source_count: int
    used_count: int
    deduplicated_count: int
    categories: list[str]
    tokens_estimate: int
    scope_expanded: bool = False  # True when canonical (domain/principle) memories were appended
    session_id: Optional[str] = None
    pending_hints: list[PendingHint] = []  # Scout best-practice candidates awaiting review
