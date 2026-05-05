from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

class TreeNode(BaseModel):
    """Узел иерархического дерева знаний (хранится в SQLite)"""
    path: str = Field(..., description="Полный путь ветки, например 'python/fastapi/auth'")
    parent_path: Optional[str] = Field(None, description="Путь родителя, например 'python/fastapi'")
    level: int = Field(..., ge=1, description="Глубина ветки (1 - ствол, 2 - ветвь и т.д.)")
    strength: float = Field(0.1, ge=0.0, le=1.0, description="Сила/вес ветки, увеличивается при использовании")
    access_count: int = Field(0, description="Количество обращений к этой ветви")
    last_accessed: datetime = Field(default_factory=_now_utc)
    is_locked: bool = Field(False, description="Если True, ветвь защищена от автоматической обрезки (pruning)")

class RoutingRule(BaseModel):
    """Статистика теневой оценки (Shadow Evaluation) для паттернов запросов"""
    pattern: str = Field(..., description="Извлеченный паттерн запроса (например 'jwt auth')")
    slm_successes: int = Field(0, description="Сколько раз локальная SLM совпала с облачной LLM")
    slm_failures: int = Field(0, description="Сколько раз локальная SLM критически ошиблась")
    requires_llm: bool = Field(False, description="Флаг: отправлять ли такие запросы сразу в LLM, минуя SLM")
