from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


TaskExecutionState = Literal[
    "planning",
    "implementation",
    "verification",
    "live_validation",
    "documentation",
    "checkpointing",
    "handoff",
    "operator_review",
]


class TaskExecutionContextRequest(BaseModel):
    project: str = Field("mnemoforge", min_length=1, max_length=128)
    task_id: str = Field("", max_length=256)
    task: str = Field(..., min_length=1, max_length=4000)
    state: TaskExecutionState
    intent: str = Field("", max_length=1000)
    changed_files: list[str] = Field(default_factory=list, max_length=100)
    prior_stage_recorded: bool | None = None
    stage_evidence: list[str] = Field(default_factory=list, max_length=50)
    include_tools: bool = True
    include_rules: bool = True
    max_required_rules: int = Field(8, ge=0, le=20)
    max_recommended_rules: int = Field(8, ge=0, le=20)


class TaskExecutionRuleRef(BaseModel):
    id: str
    title: str
    scope: str
    status: str
    topic_path: str | None = None
    rationale: str = ""
    reason: str = ""


class TaskExecutionToolSuggestion(BaseModel):
    family: str
    tools: list[str] = Field(default_factory=list)
    reason: str = ""


class TaskExecutionReadiness(BaseModel):
    ready_to_enter: bool = True
    missing_prerequisites: list[str] = Field(default_factory=list)
    required_before_entering: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""


class OperationTray(BaseModel):
    state: TaskExecutionState
    primary_tools: list[str] = Field(default_factory=list)
    assistant_tools: list[str] = Field(default_factory=list)
    diagnostic_tools: list[str] = Field(default_factory=list)
    guarded_tools: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    bureaucracy_budget: dict[str, object] = Field(default_factory=dict)
    risk_controls: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    next_transitions: list[TaskExecutionState] = Field(default_factory=list)
    reasons: dict[str, str] = Field(default_factory=dict)


class TaskExecutionContextResponse(BaseModel):
    project: str
    state: TaskExecutionState
    task: str
    intent: str = ""
    readiness: TaskExecutionReadiness = Field(default_factory=TaskExecutionReadiness)
    operation_tray: OperationTray | None = None
    required_rules: list[TaskExecutionRuleRef] = Field(default_factory=list)
    recommended_rules: list[TaskExecutionRuleRef] = Field(default_factory=list)
    recommended_tools: list[TaskExecutionToolSuggestion] = Field(default_factory=list)
    risk_controls: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    next_transitions: list[TaskExecutionState] = Field(default_factory=list)
    rationale: str = ""
    coverage: dict[str, int] = Field(default_factory=dict)
