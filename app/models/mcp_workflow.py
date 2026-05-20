from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


WorkflowStateName = Literal[
    "planning",
    "implementation",
    "verification",
    "live_validation",
    "checkpointing",
    "handoff",
    "operator_review",
]


class WorkflowToolRef(BaseModel):
    name: str = Field(..., min_length=1)
    reason: str = ""


class WorkflowForbiddenPattern(BaseModel):
    id: str = Field(..., min_length=1)
    match: list[str] = Field(default_factory=list)
    message: str = Field(..., min_length=1)


class WorkflowPacketSpec(BaseModel):
    template: str = Field(..., min_length=1)
    compact: bool = True
    max_required_rules: int = Field(default=3, ge=0, le=20)
    max_recommended_rules: int = Field(default=3, ge=0, le=20)
    include_debug: bool = False


class WorkflowTransitionSpec(BaseModel):
    to: WorkflowStateName
    when: str = Field(..., min_length=1)
    requires: list[str] = Field(default_factory=list)


class WorkflowFeatureToggleRef(BaseModel):
    id: str = Field(..., min_length=1)
    effect: Literal["disable_tool", "disable_transition", "mark_unavailable"]
    reason: str = Field(..., min_length=1)


class WorkflowStateSpec(BaseModel):
    id: WorkflowStateName
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    required_evidence: list[str] = Field(default_factory=list)
    allowed_tools: list[WorkflowToolRef] = Field(default_factory=list)
    forbidden_patterns: list[WorkflowForbiddenPattern] = Field(default_factory=list)
    feature_toggles: list[WorkflowFeatureToggleRef] = Field(default_factory=list)
    packet: WorkflowPacketSpec
    transitions: list[WorkflowTransitionSpec] = Field(default_factory=list)


class ClerkCaptureTypeSpec(BaseModel):
    id: str = Field(..., min_length=1)
    purpose: str = Field(..., min_length=1)
    anchor_tags: list[str] = Field(default_factory=list)
    review_tool: str = Field(..., min_length=1)
    mutation_tool: str = Field(..., min_length=1)


class ClerkCaptureRegistry(BaseModel):
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    capture_types: list[ClerkCaptureTypeSpec] = Field(default_factory=list)


class WorkflowFeatureToggleSpec(BaseModel):
    id: str = Field(..., min_length=1)
    default_enabled: bool = True
    scopes: list[Literal["session", "runtime_profile", "agent", "project", "global"]] = Field(default_factory=list)
    target_tools: list[str] = Field(default_factory=list)
    target_transitions: list[str] = Field(default_factory=list)
    disable_reason: str = Field(..., min_length=1)
    replacement_tools: list[str] = Field(default_factory=list)
    operator_note: str = ""


class WorkflowFeatureToggleRegistry(BaseModel):
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    toggles: list[WorkflowFeatureToggleSpec] = Field(default_factory=list)


class RuntimeFingerprintField(BaseModel):
    name: str = Field(..., min_length=1)
    source: Literal["client", "server", "workspace", "model", "operator"]
    required: bool = False
    privacy: Literal["plain", "hashed", "redacted"] = "plain"


class RuntimeProfilePreset(BaseModel):
    id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    default_disabled_features: list[str] = Field(default_factory=list)
    packet_profile: Literal["minimal", "compact", "normal", "diagnostic"] = "compact"
    allow_internal_diagnostics: bool = False


class RuntimeProfileSpec(BaseModel):
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    stable_identity_file: str = Field(..., min_length=1)
    fingerprint_fields: list[RuntimeFingerprintField] = Field(default_factory=list)
    profile_presets: list[RuntimeProfilePreset] = Field(default_factory=list)


class TaskReclaimPolicy(BaseModel):
    id: str = Field(..., min_length=1)
    purpose: str = Field(..., min_length=1)
    ownership_keys: list[str] = Field(default_factory=list)
    allow_when: list[str] = Field(default_factory=list)
    deny_when: list[str] = Field(default_factory=list)
    required_audit_events: list[str] = Field(default_factory=list)
    replacement_for: list[str] = Field(default_factory=list)


class TaskLeaseWorkflowSpec(BaseModel):
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    claim_identity_fields: list[str] = Field(default_factory=list)
    primary_ownership_key: Literal["agent_fingerprint", "agent_id", "work_token_hash"]
    work_id_semantics: str = Field(..., min_length=1)
    reclaim_policies: list[TaskReclaimPolicy] = Field(default_factory=list)


class ResponseVisibilityField(BaseModel):
    name: str = Field(..., min_length=1)
    visibility: Literal["public", "internal"]
    purpose: str = Field(..., min_length=1)


class ResponseEnvelopeSpec(BaseModel):
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    default_visibility: Literal["public", "internal"] = "public"
    public_fields: list[ResponseVisibilityField] = Field(default_factory=list)
    internal_fields: list[ResponseVisibilityField] = Field(default_factory=list)
    diagnostic_access_profiles: list[str] = Field(default_factory=list)


class MailboxFormPostconditions(BaseModel):
    expected_metadata: dict[str, str | bool] = Field(default_factory=dict)
    forbidden_metadata: dict[str, list[str | bool]] = Field(default_factory=dict)
    required_receipt_fields: list[str] = Field(default_factory=list)


class MailboxFormAssistance(BaseModel):
    clerk_available: bool = False
    can_use_stenography: bool = False
    default_mode: Literal["none", "validate", "autofill", "question"] = "none"


class MailboxFormSpec(BaseModel):
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    purpose: str = Field(..., min_length=1)
    states: list[WorkflowStateName] = Field(default_factory=list)
    mode: Literal["read", "write", "transition"] = "read"
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    assistance: MailboxFormAssistance = Field(default_factory=MailboxFormAssistance)
    feature_toggles: list[str] = Field(default_factory=list)
    replacement_form_ids: list[str] = Field(default_factory=list)
    postconditions: MailboxFormPostconditions = Field(default_factory=MailboxFormPostconditions)
    public_hint: str = ""


class MailboxProtocolAction(BaseModel):
    id: str = Field(..., min_length=1)
    purpose: str = Field(..., min_length=1)
    mutating: bool = False
    public_response: bool = True


class MailboxProtocolSpec(BaseModel):
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    external_actions: list[MailboxProtocolAction] = Field(default_factory=list)
    default_action: str = "mailbox_state"


class MailboxFormVisibilityRule(BaseModel):
    packet_profile: Literal["minimal", "compact", "normal", "diagnostic"]
    hidden_form_ids: list[str] = Field(default_factory=list)
    hide_only_when_form_ids_available: list[str] = Field(default_factory=list)
    reason: str = ""


class MailboxFormPolicySpec(BaseModel):
    version: int = Field(default=1, ge=1)
    purpose: str = Field(..., min_length=1)
    state_priorities: dict[WorkflowStateName, list[str]] = Field(default_factory=dict)
    visibility_rules: list[MailboxFormVisibilityRule] = Field(default_factory=list)
