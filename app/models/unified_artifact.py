from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID


@dataclass
class ArtifactKey:
    """Унифицированный идентификатор для improvements и tasks.

    Формат: {type}:{project}:{local_id}
    Примеры:
    - improvement:mnemoforge:dcde5e07-744a-4836-b08c-e18300eccf78
    - task:mnemoforge:6174ad7b-1fd9-4b6b-bb59-4f932b8cfc8c
    """

    type: Literal["improvement", "task"]
    project: str
    local_id: str  # UUID

    @classmethod
    def parse(cls, key: str) -> "ArtifactKey":
        """Парсить artifact_key из строки."""
        try:
            type_, project, local_id = key.split(":", 2)
            if type_ not in ("improvement", "task"):
                raise ValueError(f"Invalid artifact type: {type_}")
            return cls(type=type_, project=project, local_id=local_id)
        except ValueError as e:
            raise ValueError(
                f"Invalid artifact_key format: {key}. "
                f"Expected format: {{type}}:{{project}}:{{local_id}}. "
                f"Error: {e}"
            ) from e

    def __str__(self) -> str:
        """Преобразовать в строку."""
        return f"{self.type}:{self.project}:{self.local_id}"

    def to_uuid(self) -> UUID:
        """Преобразовать local_id в UUID."""
        try:
            return UUID(self.local_id)
        except ValueError as e:
            raise ValueError(f"Invalid UUID in artifact_key: {self.local_id}") from e


@dataclass
class UnifiedArtifactRecord:
    """Унифицированная запись для improvements и tasks."""

    artifact_key: str  # "improvement:mnemoforge:abc" or "task:mnemoforge:def"
    type: str  # "improvement" or "task"
    id: UUID  # локальный ID
    project: str
    title: str
    description: str
    status: str  # унифицированный статус
    agent_id: str
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    # Поля для improvements
    importance_score: Optional[float] = None
    stage: Optional[str] = None
    verdict: Optional[str] = None
    resolved_at: Optional[datetime] = None
    report_count: Optional[int] = None

    # Поля для tasks
    task_id: Optional[str] = None
    source: Optional[str] = None
    topic_path: Optional[str] = None
    task_capture_pending_count: Optional[int] = None
    task_capture_promoted_count: Optional[int] = None
    task_statement_incomplete: Optional[bool] = None
    query_score: Optional[float] = None
    match_reason: Optional[str] = None
    matched_topic_tags: list[str] = field(default_factory=list)

    # Связанные сущности
    linked_artifact_key: Optional[str] = None  # "task:mnemoforge:def" или "improvement:mnemoforge:abc"
    linked_status: Optional[str] = None  # статус связанной сущности


@dataclass
class UnifiedArtifactListResponse:
    """Ответ для списка unified artifacts."""

    total: int
    items: list[UnifiedArtifactRecord]
    search_mode: str = "lexical"
    backend_used: str = "sqlite_lexical"
    candidate_count: int = 0
    sqlite_validated_count: int = 0
    fallback_reason: str = ""


@dataclass
class UnifiedArtifactResolveRequest:
    """Запрос для разрешения artifact."""

    acted_by: str = "user"
    action_source: str = "inline_user_approval"
    reason: str = ""


@dataclass
class UnifiedArtifactReopenRequest:
    """Запрос для переоткрытия artifact."""

    project: str
    status: str = "active"
    reason: str = "reopen_artifact"
    acted_by: str = "user"
    action_source: str = "unified_artifact"
    source: str = "unified-artifact"  # Для tasks


# Унифицированные статусы
UNIFIED_STATUS_MAPPING = {
    # Improvement → Unified
    ("improvement", "open"): "open",
    ("improvement", "resolved"): "done",
    # Task → Unified
    ("task", "planning"): "open",
    ("task", "active"): "active",
    ("task", "done"): "done",
    ("task", "paused"): "paused",
    ("task", "archived"): "archived",
}

# Обратное маппинг для синхронизации
SYNC_STATUS_MAPPING = {
    # Improvement → Task
    ("improvement", "open"): "active",
    ("improvement", "resolved"): "done",
    # Task → Improvement
    ("task", "active"): "open",
    ("task", "done"): "resolved",
    ("task", "planning"): "open",
}


def to_unified_status(type_: str, status: str) -> str:
    """Преобразовать статус в унифицированный формат."""
    return UNIFIED_STATUS_MAPPING.get((type_, status), status)


def from_unified_status(type_: str, unified_status: str) -> str:
    """Преобразовать унифицированный статус обратно в тип-специфичный."""
    for (t, s), u in UNIFIED_STATUS_MAPPING.items():
        if t == type_ and u == unified_status:
            return s
    return unified_status
