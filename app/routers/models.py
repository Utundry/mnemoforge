"""
Model Registry API — quota management and cross-CLI task handoff.
"""
import asyncio
import re
import uuid
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep
from app.models.memory import MemoryUpdate
from app.models.coordination import (
    COORDINATION_MAILBOX_PATTERN,
    COORDINATION_STATUS_PATTERN,
    CoordinationListResponse,
    CoordinationMessageCreate,
    CoordinationMessageRecord,
    CoordinationPickupRequest,
    CoordinationStatusUpdate,
)
from app.services.coordination_service import (
    create_coordination_message,
    list_coordination_messages,
    pickup_coordination_messages,
    update_coordination_message_status,
)
from app.services.docs_service import load_docs_cache
from app.services.improvements_store import get_improvements_store
from app.services.law_service import get_project_law
from app.services.learning_store import get_learning_store
from app.services.model_registry import get_model_registry
from app.services.operational_instincts_service import build_operational_instinct_playbook
from app.services.task_router import decide as decide_task_route
from app.services.project_knowledge import ProjectKnowledgeService
from app.services.project_context_service import assemble_project_context, build_task_triage
from app.services.project_task_service import get_project_task

router = APIRouter(prefix="/models", tags=["models"])
coordination_router = APIRouter(prefix="/coordination", tags=["coordination"])
_HANDOFF_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_HANDOFF_STATUS_PATTERN = re.compile(r"^(pending|picked_up|active|paused|closed|archived)$")
_EXECUTION_MODE_PATTERN = re.compile(r"^(max_quality|balanced|economy|strict_economy)$")
_SUPPORTED_HANDOFF_REF_TYPES = (
    "laws",
    "components",
    "improvements",
    "runtime_hints",
    "tasks",
    "task_capture_candidates",
    "docs_sections",
)
_SUPPORTED_PACKET_BACKGROUND_JOB_TYPES = {
    "project_ingest",
    "project_refresh",
    "skills_retag",
    "evolve_skills",
    "regenerate_skill_content",
    "verify_tree_classification",
    "task_memoir",
    "docs_rebuild",
}


# ── Request / Response models ────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=256)
    provider: str = Field(..., min_length=1, max_length=64)
    daily_limit: int = Field(..., gt=0)
    limit_unit: str = Field("tokens", pattern="^(tokens|requests)$")
    priority: int = Field(99, ge=1)
    task_capabilities: list[str] = Field(default_factory=list)
    initial_scores: dict[str, float] = Field(default_factory=dict)
    weekly_limit: Optional[int] = Field(None, gt=0, description="Optional weekly quota override (same units as daily limit)")


class ReportUsageRequest(BaseModel):
    model_id: str
    units_used: int = Field(..., gt=0)


class ReportLimitRequest(BaseModel):
    model_id: str
    error_code: Optional[str] = None
    error_msg: Optional[str] = None
    retry_after: Optional[int] = Field(None, description="Cooldown seconds until model is available again")


class HandoffRequest(BaseModel):
    task_id: Optional[str] = Field(None, description="Unique task identifier (auto-generated if omitted)")
    handoff_label: Optional[str] = Field(None, max_length=64, description="Human-readable handoff label, e.g. benchmark28")
    from_agent: str = Field(..., description="Source CLI: claude-code | codex | cline | gemini-cli")
    to_agent: str = Field(..., description="Target CLI: claude-code | codex | cline | gemini-cli")
    project_id: Optional[str] = Field(None, max_length=128, description="Optional project scope for playbook-aware handoffs")
    owner_agent: Optional[str] = Field(None, max_length=128, description="Agent currently responsible for this task packet; defaults to to_agent")
    write_scope: list[str] = Field(default_factory=list, max_length=20, description="Bounded files/modules/areas this packet is expected to modify or own")
    phase: Optional[str] = Field(None, max_length=64, description="Current task lifecycle phase, e.g. task_framing")
    priority: Optional[str] = Field(None, max_length=32, description="Human-readable priority like high, medium, low")
    why_now: Optional[str] = Field(None, max_length=500, description="Why this handoff matters now")
    definition_of_done: Optional[str] = Field(None, max_length=1000, description="What counts as sufficient completion for this iteration")
    expected_output_shape: Optional[str] = Field(None, max_length=1000, description="Expected shape of the receiving agent's output")
    phase_objective: Optional[str] = Field(None, max_length=1000, description="Objective of the current task lifecycle phase")
    execution_mode: str = Field("balanced", pattern=r"^(max_quality|balanced|economy|strict_economy)$", description="Execution policy mode that biases cost/quality routing for this packet")
    background_job_type: Optional[str] = Field(None, max_length=128, description="Optional existing background job type for safe queued execution")
    background_payload: dict[str, Any] = Field(default_factory=dict, description="Optional payload for background job dispatch")
    suggested_execution_tier: Optional[str] = Field(None, max_length=32, description="Suggested execution tier like local, mini, standard, frontier")
    model_hint: Optional[str] = Field(None, max_length=500, description="Compact guidance about what model tier best fits this packet")
    core_instinct_ids: list[str] = Field(default_factory=list, max_length=20, description="Core instincts relevant to this handoff phase")
    supporting_instinct_ids: list[str] = Field(default_factory=list, max_length=20, description="Supporting instincts relevant to this handoff phase")
    project_context_summary: Optional[str] = Field(None, max_length=2000, description="Short inline summary of reusable project context")
    project_context_refs: dict[str, list[str]] = Field(default_factory=dict, description="Typed references to reusable project knowledge objects")
    project_context_snapshot: Optional[str] = Field(None, max_length=4000, description="Optional compact project context bundle for the receiving agent")
    from_model_id: Optional[str] = Field(
        None,
        description="Optional cloud model/component that hit a rate/quota limit, e.g. claude-sonnet",
    )
    task_description: str = Field(..., min_length=1, max_length=2000)
    partial_result: Optional[str] = Field(None, max_length=5000)
    key_facts: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field("manual", description="manual | limit_hit")
    agent_id: str = Field("handoff", description="Memory agent_id for Qdrant storage")


class PickupRequest(BaseModel):
    agent_id: str = Field(..., description="This CLI's identity matches to_agent in stored handoffs")
    handoff_label: Optional[str] = Field(None, max_length=64, description="Optional human-readable label to narrow pickup")
    limit: int = Field(3, ge=1, le=20)


class ListHandoffsRequest(BaseModel):
    agent_id: str = Field(..., description="This CLI's identity matches to_agent in stored handoffs")
    statuses: list[str] = Field(default_factory=list, description="Lifecycle statuses to include")
    handoff_label: Optional[str] = Field(None, max_length=64, description="Optional human-readable label to narrow listing")
    owner_agent: Optional[str] = Field(None, max_length=128, description="Optional current owner to filter task packets")
    write_scope: list[str] = Field(default_factory=list, max_length=20, description="Optional required write-scope entries to filter task packets")
    limit: int = Field(20, ge=1, le=100)
    compact: bool = Field(True, description="Return compact task-packet summaries instead of raw full payloads")


class HandoffWorkspaceSummaryRequest(BaseModel):
    agent_id: str = Field(..., description="This CLI's identity matches to_agent in stored handoffs")
    statuses: list[str] = Field(default_factory=list, description="Lifecycle statuses to include")
    handoff_label: Optional[str] = Field(None, max_length=64, description="Optional human-readable label to narrow summary")
    owner_agent: Optional[str] = Field(None, max_length=128, description="Optional current owner to narrow summary")
    write_scope: list[str] = Field(default_factory=list, max_length=20, description="Optional required write-scope entries to narrow summary")
    packet_limit: int = Field(5, ge=1, le=20, description="How many recent compact packets to include")


class ExpandHandoffRefsRequest(BaseModel):
    memory_id: str = Field(..., description="Handoff memory UUID")
    ref_types: list[str] = Field(default_factory=list, description="Optional subset of ref types to expand")
    limit_per_type: int = Field(3, ge=1, le=10, description="Max resolved items to return per ref type")


class RefreshHandoffContextRequest(BaseModel):
    memory_id: str = Field(..., description="Handoff memory UUID")
    task_description: Optional[str] = Field(None, max_length=2000, description="Optional task text override for context refresh")
    max_components: int = Field(3, ge=1, le=10)


class HandoffStatusUpdateRequest(BaseModel):
    memory_id: str = Field(..., description="Handoff memory UUID")
    status: str = Field(..., pattern=r"^(pending|picked_up|active|paused|closed|archived)$")
    acted_by: str = Field("user", min_length=1, max_length=256)
    reason: str = Field("", max_length=500)
    owner_agent: Optional[str] = Field(None, max_length=128, description="Optional ownership transfer while changing packet status")
    write_scope: list[str] = Field(default_factory=list, max_length=20, description="Optional bounded write scope to persist on the packet")
    executor_used: Optional[str] = Field(None, max_length=128, description="Actual executor used for this packet, e.g. cheap_subagent or local_slm_background")
    model_used: Optional[str] = Field(None, max_length=128, description="Actual model/component used during execution, e.g. gpt-5.4-mini or qwen3:1.7b")
    result_summary: Optional[str] = Field(None, max_length=1000, description="Short summary of the bounded result being merged back")
    verification_summary: Optional[str] = Field(None, max_length=1000, description="Short verification note for the bounded result")


class ResumeHandoffRequest(BaseModel):
    memory_id: str = Field(..., description="Handoff memory UUID")
    refresh_context: bool = Field(True, description="Refresh compact project context on resume when possible")
    task_description: Optional[str] = Field(None, max_length=2000, description="Optional task text override for refresh")
    max_components: int = Field(3, ge=1, le=10)
    acted_by: str = Field("user", min_length=1, max_length=256)
    reason: str = Field("resume", max_length=500)
    owner_agent: Optional[str] = Field(None, max_length=128, description="Optional new owner for the resumed packet")
    write_scope: list[str] = Field(default_factory=list, max_length=20, description="Optional bounded write scope for the resumed packet")


class DecomposeTaskPacketRequest(BaseModel):
    project_id: Optional[str] = Field(None, max_length=128, description="Optional project scope for project-aware playbook guidance")
    task_description: str = Field(..., min_length=1, max_length=2000, description="The larger task to split into bounded packets")
    handoff_label_prefix: Optional[str] = Field(None, max_length=48, description="Optional human-readable prefix for suggested packet labels")
    phase: Optional[str] = Field(None, max_length=64, description="Preferred task lifecycle phase for the suggested packets")
    priority: Optional[str] = Field(None, max_length=32, description="Priority to copy into the recommended packet stubs")
    owner_agent: Optional[str] = Field(None, max_length=128, description="Optional default owner to assign to the suggested packets")
    execution_mode: str = Field("balanced", pattern=r"^(max_quality|balanced|economy|strict_economy)$", description="Policy mode that biases decomposition toward quality or economy")
    write_scope: list[str] = Field(default_factory=list, max_length=20, description="Candidate bounded write scopes or work areas to use for splitting")
    max_packets: int = Field(4, ge=1, le=8, description="Maximum number of packet stubs to recommend")


class TaskPacketStub(BaseModel):
    handoff_label: Optional[str] = Field(None, max_length=64)
    task_description: Optional[str] = Field(None, max_length=2000)
    owner_agent: Optional[str] = Field(None, max_length=128)
    write_scope: list[str] = Field(default_factory=list, max_length=20)
    phase: Optional[str] = Field(None, max_length=64)
    priority: Optional[str] = Field(None, max_length=32)
    why_now: Optional[str] = Field(None, max_length=500)
    definition_of_done: Optional[str] = Field(None, max_length=1000)
    expected_output_shape: Optional[str] = Field(None, max_length=1000)
    phase_objective: Optional[str] = Field(None, max_length=1000)
    execution_mode: Optional[str] = Field(None, pattern=r"^(max_quality|balanced|economy|strict_economy)$")
    background_job_type: Optional[str] = Field(None, max_length=128)
    background_payload: dict[str, Any] = Field(default_factory=dict)
    suggested_execution_tier: Optional[str] = Field(None, max_length=32)
    model_hint: Optional[str] = Field(None, max_length=500)
    core_instinct_ids: list[str] = Field(default_factory=list, max_length=20)
    supporting_instinct_ids: list[str] = Field(default_factory=list, max_length=20)
    project_context_summary: Optional[str] = Field(None, max_length=2000)
    project_context_refs: dict[str, list[str]] = Field(default_factory=dict)
    project_context_snapshot: Optional[str] = Field(None, max_length=4000)


class CreateTaskPacketsRequest(BaseModel):
    from_agent: str = Field(..., description="Source CLI: claude-code | codex | cline | gemini-cli")
    to_agent: str = Field(..., description="Target CLI: claude-code | codex | cline | gemini-cli")
    project_id: Optional[str] = Field(None, max_length=128)
    task_description: str = Field(..., min_length=1, max_length=2000, description="Fallback task description used when a packet stub does not override it")
    execution_mode: str = Field("balanced", pattern=r"^(max_quality|balanced|economy|strict_economy)$", description="Fallback execution policy mode used when a packet stub does not override it")
    partial_result: Optional[str] = Field(None, max_length=5000)
    key_facts: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field("manual", description="manual | limit_hit")
    from_model_id: Optional[str] = Field(None, description="Optional originating cloud model/component")
    packets: list[TaskPacketStub] = Field(..., min_length=1, max_length=8, description="Recommended packet stubs to materialize as real task packets")
    agent_id: str = Field("handoff", description="Memory agent_id for Qdrant storage")


class RouteTaskPacketExecutionRequest(BaseModel):
    memory_id: Optional[str] = Field(None, description="Existing handoff memory UUID")
    packet: Optional[TaskPacketStub] = Field(None, description="Inline packet stub to evaluate before creation")
    execution_mode: Optional[str] = Field(None, pattern=r"^(max_quality|balanced|economy|strict_economy)$", description="Optional override for routing policy mode")


class DispatchBackgroundPacketRequest(BaseModel):
    memory_id: str = Field(..., description="Existing handoff memory UUID to dispatch through the background job queue")
    acted_by: str = Field("user", min_length=1, max_length=256)
    reason: str = Field("background_dispatch", max_length=500)


class ReconcileBackgroundPacketRequest(BaseModel):
    memory_id: str = Field(..., description="Existing handoff memory UUID with a dispatched background job")
    acted_by: str = Field("user", min_length=1, max_length=256)
    reason: str = Field("background_reconcile", max_length=500)


def _normalize_handoff_label(value: str | None) -> str | None:
    if value is None:
        return None
    label = value.strip().lower()
    if not label:
        return None
    if not _HANDOFF_LABEL_PATTERN.fullmatch(label):
        raise HTTPException(status_code=400, detail="Invalid handoff_label format")
    return label


def _extract_content_value(content: str, key: str) -> str:
    prefix = f"{key}:"
    for line in (content or "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _extract_content_csv(content: str, key: str) -> list[str]:
    raw = _extract_content_value(content, key)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _extract_content_multiline(content: str, key: str) -> str:
    prefix = f"{key}:"
    lines = (content or "").splitlines()
    captured: list[str] = []
    collecting = False
    for line in lines:
        if not collecting:
            if line.startswith(prefix):
                inline = line[len(prefix):].strip()
                if inline:
                    captured.append(inline)
                collecting = True
            continue
        if line and line[0].isalpha() and ":" in line:
            candidate_key = line.split(":", 1)[0]
            if candidate_key.replace("_", "").isalnum():
                break
        captured.append(line)
    return "\n".join(captured).strip()


def _normalize_ref_types(values: list[str]) -> list[str]:
    requested: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if key and key in _SUPPORTED_HANDOFF_REF_TYPES and key not in requested:
            requested.append(key)
    return requested


def _normalize_handoff_statuses(values: list[str]) -> list[str]:
    requested: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if key == "all":
            return []
        if key and _HANDOFF_STATUS_PATTERN.fullmatch(key) and key not in requested:
            requested.append(key)
    return requested


def _normalize_execution_mode(value: str | None, *, default: str = "balanced") -> str:
    mode = str(value or "").strip().lower() or default
    if not _EXECUTION_MODE_PATTERN.fullmatch(mode):
        raise HTTPException(status_code=400, detail="Invalid execution_mode")
    return mode


def _normalize_write_scope(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        if len(item) > 256:
            item = item[:256].rstrip()
        if item not in normalized:
            normalized.append(item)
        if len(normalized) >= 20:
            break
    return normalized


def _slugify_handoff_label_prefix(value: str | None, *, fallback: str = "packet") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    if not text:
        return fallback
    return text[:32]


def _candidate_write_scope_label(scope: str, index: int) -> str:
    raw = str(scope or "").strip()
    if not raw:
        return f"slice-{index}"
    tail = raw.replace("\\", "/").split("/")[-1]
    label = re.sub(r"[^a-z0-9_-]+", "-", tail.lower()).strip("-_")
    return (label or f"slice-{index}")[:24]


def _chunk_write_scopes(write_scope: list[str], max_packets: int) -> list[list[str]]:
    scopes = [item for item in write_scope if item]
    if not scopes:
        return []
    if len(scopes) <= max_packets:
        return [[scope] for scope in scopes]
    packet_count = max(1, min(max_packets, len(scopes)))
    tokenized: list[tuple[str, set[str]]] = []
    for scope in scopes:
        normalized = scope.replace("\\", "/").lower()
        tokens = {token for token in re.split(r"[^a-z0-9]+", normalized) if token}
        tokenized.append((scope, tokens))
    clusters: list[list[tuple[str, set[str]]]] = [[item] for item in tokenized[:packet_count]]
    for scope, tokens in tokenized[packet_count:]:
        best_index = 0
        best_score = -1.0
        for index, cluster in enumerate(clusters):
            cluster_tokens: set[str] = set()
            for _, cluster_token_set in cluster:
                cluster_tokens.update(cluster_token_set)
            if not cluster_tokens and not tokens:
                score = 0.0
            else:
                overlap = len(cluster_tokens & tokens)
                union = len(cluster_tokens | tokens) or 1
                score = overlap / union
            if score > best_score:
                best_score = score
                best_index = index
        clusters[best_index].append((scope, tokens))
    return [[scope for scope, _ in cluster] for cluster in clusters if cluster]


def _build_packet_decomposition_strategy(*, write_scope: list[str], max_packets: int) -> str:
    if len(write_scope) <= 1:
        return "single_packet"
    if len(write_scope) <= max_packets:
        return "split_by_write_scope"
    return "grouped_write_scope"


def _build_packet_decomposition_why_split(*, strategy: str, write_scope: list[str]) -> str:
    if strategy == "single_packet":
        return "Current task looks bounded enough for one packet; no parallel split is recommended yet."
    if strategy == "split_by_write_scope":
        return "Suggested packets are separated by bounded write scopes so they can be delegated, paused, or merged back with lower conflict risk."
    return "Suggested packets group several write scopes to keep the packet count bounded while still reducing merge risk."


def _estimate_packet_execution_tier(
    *,
    phase: str,
    write_scope: list[str],
    task_description: str,
    execution_mode: str,
) -> tuple[str, str]:
    mode = _normalize_execution_mode(execution_mode)
    text = " ".join([phase, task_description, *write_scope]).lower()
    deterministic_markers = (
        "test",
        "schema",
        "format",
        "parity",
        "wiring",
        "inventory",
        "list",
        "summary",
        "docs",
        "mcp",
        "router",
        "server",
    )
    architecture_markers = (
        "architecture",
        "strategy",
        "benchmark",
        "roadmap",
        "compare",
        "tradeoff",
        "decompose",
        "design",
    )
    if phase in {"idea_capture", "task_framing", "option_selection"} and not write_scope:
        if mode in {"economy", "strict_economy"}:
            return (
                "standard",
                "Keep ambiguous planning packets on a standard tier in economy mode; only escalate above that with an explicit override.",
            )
        return (
            "frontier",
            "Use a strong reasoning model when the task is still ambiguous and the packet is not yet bounded by write scope.",
        )
    if all(scope.startswith(("tests/", "docs/", "demo/")) for scope in write_scope if scope) and len(write_scope) <= 2:
        return (
            "local",
            "Prefer a local or very cheap model when the packet is narrow, deterministic, and easy to verify with focused tests or docs review.",
        )
    if any(marker in text for marker in deterministic_markers) and len(write_scope) <= 3:
        if mode == "strict_economy":
            return (
                "local",
                "Strict economy mode prefers the local or cheapest possible tier for narrow deterministic packets that are easy to verify.",
            )
        return (
            "mini",
            "A mini or cheap cloud model should be enough because this packet is narrow and the result is easy to verify.",
        )
    if phase in {"task_framing", "option_selection"} or any(marker in text for marker in architecture_markers):
        if mode in {"economy", "strict_economy"}:
            return (
                "standard",
                "Economy mode avoids frontier by default even for strategic packets; escalate only when the cheaper reviewed tier is insufficient.",
            )
        return (
            "frontier",
            "Prefer a frontier model when the packet still depends on option ranking, strategic tradeoffs, or architectural ambiguity.",
        )
    if mode in {"economy", "strict_economy"}:
        return (
            "mini",
            "Economy mode prefers a mini or otherwise cheaper verifiable tier for bounded implementation packets.",
        )
    return (
        "standard",
        "Use a standard model tier for bounded implementation work that still needs moderate repository context or integration judgment.",
    )


def _build_decomposition_packet_stub(
    *,
    label_prefix: str,
    phase: str,
    priority: str | None,
    owner_agent: str | None,
    packet_index: int,
    scopes: list[str],
    objective: str,
    core_instinct_ids: list[str],
    supporting_instinct_ids: list[str],
    task_description: str,
    execution_mode: str,
) -> dict[str, Any]:
    scope_label = _candidate_write_scope_label(scopes[0] if scopes else "", packet_index)
    suggested_execution_tier, model_hint = _estimate_packet_execution_tier(
        phase=phase,
        write_scope=scopes,
        task_description=task_description,
        execution_mode=execution_mode,
    )
    return {
        "handoff_label": f"{label_prefix}-{scope_label}"[:48],
        "phase": phase,
        "priority": priority or "medium",
        "owner_agent": owner_agent,
        "write_scope": scopes,
        "phase_objective": objective,
        "execution_mode": execution_mode,
        "suggested_execution_tier": suggested_execution_tier,
        "model_hint": model_hint,
        "definition_of_done": (
            "Finish the bounded work for this packet, verify the result, and return a short merge-back summary."
        ),
        "expected_output_shape": (
            "Short result summary, verification summary, and any follow-up or merge-back notes."
        ),
        "why_this_packet": (
            "This packet narrows work to a mergeable write scope so it can be delegated or resumed without dragging the whole task context."
        ),
        "core_instinct_ids": core_instinct_ids,
        "supporting_instinct_ids": supporting_instinct_ids,
    }


async def _create_handoff_record(
    *,
    body: HandoffRequest,
    qdrant,
    ollama,
) -> dict[str, Any]:
    task_id = body.task_id or str(uuid.uuid4())[:8]
    handoff_label = _normalize_handoff_label(body.handoff_label)
    execution_mode = _normalize_execution_mode(body.execution_mode)
    owner_agent = (body.owner_agent or body.to_agent).strip()
    write_scope = _normalize_write_scope(body.write_scope)

    facts_text = "\n".join(f"- {f}" for f in body.key_facts[:10]) if body.key_facts else ""
    content_parts = [
        "HANDOFF CONTEXT",
        f"task_id: {task_id}",
        *([f"handoff_label: {handoff_label}"] if handoff_label else []),
        f"from_agent: {body.from_agent}",
        f"to_agent: {body.to_agent}",
        f"owner_agent: {owner_agent[:128]}",
        f"reason: {body.reason}",
        f"task: {body.task_description[:500]}",
    ]
    if write_scope:
        content_parts.append("write_scope: " + ", ".join(write_scope))
    if body.phase:
        content_parts.append(f"phase: {body.phase}")
    if body.project_id:
        content_parts.append(f"project_id: {body.project_id}")
    if body.priority:
        content_parts.append(f"priority: {body.priority}")
    if body.why_now:
        content_parts.append(f"why_now: {body.why_now[:500]}")
    if body.definition_of_done:
        content_parts.append(f"definition_of_done: {body.definition_of_done[:1000]}")
    if body.expected_output_shape:
        content_parts.append(f"expected_output_shape: {body.expected_output_shape[:1000]}")
    if body.phase_objective:
        content_parts.append(f"phase_objective: {body.phase_objective[:1000]}")
    if execution_mode:
        content_parts.append(f"execution_mode: {execution_mode}")
    if body.background_job_type:
        content_parts.append(f"background_job_type: {body.background_job_type[:128]}")
    if body.background_payload:
        content_parts.append(f"background_payload: {str(body.background_payload)[:1000]}")
    if body.suggested_execution_tier:
        content_parts.append(f"suggested_execution_tier: {body.suggested_execution_tier[:32]}")
    if body.model_hint:
        content_parts.append(f"model_hint: {body.model_hint[:500]}")
    if body.core_instinct_ids:
        content_parts.append("core_instinct_ids: " + ", ".join(body.core_instinct_ids[:20]))
    if body.supporting_instinct_ids:
        content_parts.append("supporting_instinct_ids: " + ", ".join(body.supporting_instinct_ids[:20]))
    if body.from_model_id:
        content_parts.append(f"from_model_id: {body.from_model_id}")
    if body.partial_result:
        content_parts.append(f"partial_result: {body.partial_result[:1000]}")
    if facts_text:
        content_parts.append(f"key_facts:\n{facts_text}")
    if body.project_context_summary:
        content_parts.append(f"project_context_summary: {body.project_context_summary[:2000]}")
    if body.project_context_refs:
        ref_parts = []
        for key, values in body.project_context_refs.items():
            cleaned = [str(value).strip() for value in values if str(value).strip()]
            if cleaned:
                ref_parts.append(f"{key}={','.join(cleaned[:10])}")
        if ref_parts:
            content_parts.append("project_context_refs: " + "; ".join(ref_parts))
    if body.project_context_snapshot:
        content_parts.append(f"project_context_snapshot:\n{body.project_context_snapshot[:4000]}")
    content = "\n".join(content_parts)

    from app.models.enums import MemoryType
    from app.models.memory import MemoryCreate

    vector = await ollama.embed(content)
    mem = MemoryCreate(
        content=content,
        agent_id=body.agent_id,
        memory_type=MemoryType.context,
        category="handoff",
        importance_score=0.9,
        source=f"handoff:{body.from_agent}",
        tags=[
            f"to:{body.to_agent}",
            f"from:{body.from_agent}",
            *([f"from_model:{body.from_model_id}"] if body.from_model_id else []),
            *([f"handoff_label:{handoff_label}"] if handoff_label else []),
            task_id,
            body.reason,
        ],
        session_id=task_id,
        meta={
            "from_agent": body.from_agent,
            "to_agent": body.to_agent,
            "handoff_label": handoff_label,
            "reason": body.reason,
            "task_description": body.task_description[:500],
            "project_id": body.project_id,
            "owner_agent": owner_agent[:128],
            "write_scope": write_scope,
            "phase": body.phase[:64] if body.phase else None,
            "priority": body.priority[:32] if body.priority else None,
            "why_now": body.why_now[:500] if body.why_now else None,
            "definition_of_done": body.definition_of_done[:1000] if body.definition_of_done else None,
            "expected_output_shape": body.expected_output_shape[:1000] if body.expected_output_shape else None,
            "phase_objective": body.phase_objective[:1000] if body.phase_objective else None,
            "execution_mode": execution_mode,
            "background_job_type": body.background_job_type[:128] if body.background_job_type else None,
            "background_payload": body.background_payload or {},
            "suggested_execution_tier": body.suggested_execution_tier[:32] if body.suggested_execution_tier else None,
            "model_hint": body.model_hint[:500] if body.model_hint else None,
            "core_instinct_ids": body.core_instinct_ids[:20],
            "supporting_instinct_ids": body.supporting_instinct_ids[:20],
            "from_model_id": body.from_model_id,
            "partial_result": body.partial_result[:1000] if body.partial_result else None,
            "key_facts": body.key_facts[:10],
            "project_context_summary": body.project_context_summary[:2000] if body.project_context_summary else None,
            "project_context_refs": body.project_context_refs or {},
            "project_context_snapshot": body.project_context_snapshot[:4000] if body.project_context_snapshot else None,
        },
    )
    memory_id = await qdrant.insert(mem, vector)
    await qdrant.mark_handoff_pending(memory_id)

    reg = get_model_registry()
    reg.log_handoff(
        task_id=task_id,
        handoff_label=handoff_label,
        from_agent=body.from_agent,
        to_agent=body.to_agent,
        memory_id=str(memory_id),
        reason=body.reason,
    )

    if body.reason == "limit_hit" and body.from_model_id and body.from_model_id in reg._models:
        reg.report_limit_hit(body.from_model_id)

    ranked = reg.rank_for_task("code_generation")
    next_available = [
        {"model_id": model_id, "score": round(score, 3)}
        for model_id, score in ranked[:3]
        if model_id != body.from_model_id
    ]

    return {
        "memory_id": str(memory_id),
        "status": "pending",
        "task_id": task_id,
        "handoff_label": handoff_label,
        "from_agent": body.from_agent,
        "to_agent": body.to_agent,
        "project_id": body.project_id,
        "owner_agent": owner_agent[:128],
        "write_scope": write_scope,
        "phase": body.phase,
        "priority": body.priority,
        "why_now": body.why_now,
        "definition_of_done": body.definition_of_done,
        "expected_output_shape": body.expected_output_shape,
        "phase_objective": body.phase_objective,
        "execution_mode": execution_mode,
        "background_job_type": body.background_job_type,
        "background_payload": body.background_payload,
        "suggested_execution_tier": body.suggested_execution_tier,
        "model_hint": body.model_hint,
        "core_instinct_ids": body.core_instinct_ids,
        "supporting_instinct_ids": body.supporting_instinct_ids,
        "project_context_summary": body.project_context_summary,
        "project_context_refs": body.project_context_refs,
        "project_context_snapshot": body.project_context_snapshot,
        "from_model_id": body.from_model_id,
        "next_available": next_available,
        "pickup_instruction": (
            f"In {body.to_agent}: use pickup_handoff(agent_id='{body.to_agent}', handoff_label='{handoff_label}')"
            if handoff_label
            else f"In {body.to_agent}: use pickup_handoff(agent_id='{body.to_agent}')"
        ),
    }


def _preferred_tier_for_packet_execution(suggested_execution_tier: str | None) -> str | None:
    tier = str(suggested_execution_tier or "").strip().lower()
    if tier == "local":
        return "local"
    if tier in {"mini", "standard", "frontier"}:
        return "cloud"
    return None


def _recommended_model_for_executor(
    *,
    recommended_executor: str,
    routing: dict[str, Any],
) -> str | None:
    component = str(routing.get("component") or "").strip()
    if recommended_executor == "main_agent":
        return None
    if recommended_executor in {"cheap_subagent", "background_llm"}:
        if component and component != "cloud-llm":
            return component
        fallbacks = routing.get("cloud_fallbacks") or []
        if isinstance(fallbacks, list):
            for item in fallbacks:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("model_id") or "").strip()
                if model_id:
                    return model_id
    if recommended_executor == "local_slm_background":
        return "qwen3:1.7b"
    return None


def _build_packet_execution_profile(packet: dict[str, Any]) -> dict[str, Any]:
    execution_mode = _normalize_execution_mode(packet.get("execution_mode"), default="balanced")
    write_scope = [str(item).strip() for item in (packet.get("write_scope") or []) if str(item).strip()]
    task_text = " ".join(
        str(value or "").strip()
        for value in (
            packet.get("task_description"),
            packet.get("definition_of_done"),
            packet.get("expected_output_shape"),
            packet.get("model_hint"),
        )
        if str(value or "").strip()
    ).lower()
    read_only = not write_scope
    if write_scope and all(scope.startswith(("tests/", "docs/", "demo/")) for scope in write_scope):
        read_only = True
    verification_easy = any(
        marker in task_text
        for marker in ("test", "summary", "schema", "format", "inventory", "list", "parity", "verify")
    ) or read_only
    proposal_only = any(
        marker in task_text
        for marker in (
            "proposal",
            "proposed patch",
            "patch proposal",
            "patch outline",
            "candidate patch",
            "candidate changes",
            "implementation plan",
            "reviewable patch",
            "structured patch",
        )
    )
    bounded_code = bool(write_scope) and len(write_scope) <= 4
    return {
        "execution_mode": execution_mode,
        "read_only": read_only,
        "verification_easy": verification_easy,
        "proposal_only": proposal_only,
        "bounded_code": bounded_code,
        "write_scope_count": len(write_scope),
    }


def _build_packet_execution_options(
    *,
    packet: dict[str, Any],
    profile: dict[str, Any],
    routing: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, str]:
    suggested_tier = str(packet.get("suggested_execution_tier") or "").strip().lower()
    execution_mode = _normalize_execution_mode(packet.get("execution_mode"), default="balanced")
    eligible: list[dict[str, Any]] = []

    cheap_supported = bool(
        profile["bounded_code"]
        and (profile["verification_easy"] or profile["proposal_only"])
        and suggested_tier in {"mini", "standard"}
    )
    eligible.append(
        {
            "executor": "cheap_subagent",
            "supported": cheap_supported,
            "reason": (
                "Bounded scope plus easy verification or proposal-only output make this packet a good fit for a cheaper delegated agent."
                if cheap_supported
                else "Use cheap subagents only for bounded packets that are easy to verify and merge back, or for proposal-only outputs."
            ),
        }
    )

    local_supported = bool(
        profile["read_only"]
        and profile["verification_easy"]
        and suggested_tier in {"local", "mini", "standard"}
        and execution_mode in {"economy", "strict_economy", "balanced"}
    )
    eligible.append(
        {
            "executor": "local_slm_background",
            "supported": local_supported,
            "reason": (
                "Read-only or low-risk packet with cheap verification fits the local SLM background tier."
                if local_supported
                else "Reserve local SLM background execution for read-only or otherwise low-risk packets."
            ),
        }
    )

    background_supported = bool(
        profile["read_only"]
        and suggested_tier in {"mini", "standard", "frontier"}
        and execution_mode in {"balanced", "economy", "strict_economy"}
    )
    eligible.append(
        {
            "executor": "background_llm",
            "supported": background_supported,
            "reason": (
                "Background cloud LLM is suitable for read-only packets when latency is acceptable and operator interaction is not needed."
                if background_supported
                else "Do not send write-bearing packets to background LLM execution until merge-back semantics are stronger."
            ),
        }
    )

    eligible.append(
        {
            "executor": "main_agent",
            "supported": True,
            "reason": "Main agent remains the fallback when a packet is ambiguous, risky, or not yet routable to cheaper tiers.",
        }
    )

    if execution_mode == "strict_economy" and local_supported:
        return eligible, "local_slm_background", "Strict economy mode prefers local background execution first for low-risk packets that are easy to verify."
    if cheap_supported:
        return eligible, "cheap_subagent", "Packet is bounded and either easy to verify or explicitly proposal-only, so a cheap delegated external model is preferred over keeping the work on the main reasoning agent."
    if local_supported:
        return eligible, "local_slm_background", "Packet is read-only and cheap to verify, so local SLM background execution is preferred."
    if background_supported:
        return eligible, "background_llm", "Packet is read-only but may still need a stronger background model than local SLM."
    if execution_mode == "max_quality" and routing["tier"] == "cloud":
        return eligible, "main_agent", "Max quality mode keeps ambiguous or non-trivial packets on the main agent unless a stronger reviewed route is explicitly chosen."
    return eligible, "main_agent", f"Packet should stay on the main agent because current routing basis is {routing['tier']} and no cheaper executor is safely eligible."


async def _route_task_packet_execution(
    *,
    qdrant,
    body: RouteTaskPacketExecutionRequest,
) -> dict[str, Any]:
    packet: dict[str, Any]
    memory_id: str | None = None
    if body.memory_id:
        try:
            packet_uuid = UUID(str(body.memory_id))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid memory_id") from exc
        record = await qdrant.get(packet_uuid)
        if record.category != "handoff":
            raise HTTPException(status_code=404, detail="Handoff not found")
        packet = {
            "memory_id": str(record.id),
            "task_description": _extract_content_value(record.content or "", "task"),
            "write_scope": (record.meta or {}).get("write_scope") or _extract_content_csv(record.content or "", "write_scope"),
            "phase": _extract_content_value(record.content or "", "phase"),
            "definition_of_done": _extract_content_value(record.content or "", "definition_of_done"),
            "expected_output_shape": _extract_content_value(record.content or "", "expected_output_shape"),
            "execution_mode": str((record.meta or {}).get("execution_mode") or _extract_content_value(record.content or "", "execution_mode") or "").strip(),
            "background_job_type": str((record.meta or {}).get("background_job_type") or _extract_content_value(record.content or "", "background_job_type") or "").strip(),
            "background_payload": dict((record.meta or {}).get("background_payload") or {}),
            "suggested_execution_tier": str((record.meta or {}).get("suggested_execution_tier") or _extract_content_value(record.content or "", "suggested_execution_tier") or "").strip(),
            "model_hint": str((record.meta or {}).get("model_hint") or _extract_content_value(record.content or "", "model_hint") or "").strip(),
        }
        memory_id = str(record.id)
    elif body.packet is not None:
        packet = body.packet.model_dump()
    else:
        raise HTTPException(status_code=400, detail="Provide memory_id or packet")

    if body.execution_mode:
        packet["execution_mode"] = _normalize_execution_mode(body.execution_mode)
    else:
        packet["execution_mode"] = _normalize_execution_mode(packet.get("execution_mode"), default="balanced")

    task_description = str(packet.get("task_description") or "").strip()
    if not task_description:
        raise HTTPException(status_code=400, detail="Packet task_description is required for routing")

    routing_decision = await decide_task_route(
        task=task_description,
        preferred_tier=_preferred_tier_for_packet_execution(packet.get("suggested_execution_tier")),
    )
    routing = {
        "task_type": routing_decision.task_type,
        "component": routing_decision.component,
        "tier": routing_decision.tier,
        "score": round(routing_decision.score, 3),
        "confidence": round(routing_decision.confidence, 3),
        "reasoning": routing_decision.reasoning,
        "cloud_fallbacks": routing_decision.cloud_fallbacks,
    }
    profile = _build_packet_execution_profile(packet)
    eligible_executors, recommended_executor, recommendation_reason = _build_packet_execution_options(
        packet=packet,
        profile=profile,
        routing=routing,
    )
    recommended_model = _recommended_model_for_executor(
        recommended_executor=recommended_executor,
        routing=routing,
    )
    return {
        "memory_id": memory_id,
        "packet": {
            "task_description": task_description,
            "phase": packet.get("phase"),
            "write_scope": packet.get("write_scope") or [],
            "definition_of_done": packet.get("definition_of_done"),
            "expected_output_shape": packet.get("expected_output_shape"),
            "execution_mode": packet.get("execution_mode"),
            "background_job_type": packet.get("background_job_type"),
            "suggested_execution_tier": packet.get("suggested_execution_tier"),
            "model_hint": packet.get("model_hint"),
        },
        "packet_profile": profile,
        "routing_basis": routing,
        "eligible_executors": eligible_executors,
        "recommended_executor": recommended_executor,
        "recommended_model": recommended_model,
        "recommendation_reason": recommendation_reason,
    }


async def _dispatch_background_task_packet(
    *,
    qdrant,
    body: DispatchBackgroundPacketRequest,
) -> dict[str, Any]:
    try:
        packet_uuid = UUID(str(body.memory_id))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid memory_id") from exc

    record = await qdrant.get(packet_uuid)
    if record.category != "handoff":
        raise HTTPException(status_code=404, detail="Handoff not found")

    packet = {
        "memory_id": str(record.id),
        "task_description": _extract_content_value(record.content or "", "task"),
        "write_scope": (record.meta or {}).get("write_scope") or _extract_content_csv(record.content or "", "write_scope"),
        "phase": _extract_content_value(record.content or "", "phase"),
        "priority": _extract_content_value(record.content or "", "priority"),
        "why_now": _extract_content_value(record.content or "", "why_now"),
        "definition_of_done": _extract_content_value(record.content or "", "definition_of_done"),
        "expected_output_shape": _extract_content_value(record.content or "", "expected_output_shape"),
        "phase_objective": _extract_content_value(record.content or "", "phase_objective"),
        "execution_mode": str((record.meta or {}).get("execution_mode") or _extract_content_value(record.content or "", "execution_mode") or "").strip(),
        "background_job_type": str((record.meta or {}).get("background_job_type") or _extract_content_value(record.content or "", "background_job_type") or "").strip(),
        "background_payload": dict((record.meta or {}).get("background_payload") or {}),
        "suggested_execution_tier": str((record.meta or {}).get("suggested_execution_tier") or _extract_content_value(record.content or "", "suggested_execution_tier") or "").strip(),
        "model_hint": str((record.meta or {}).get("model_hint") or _extract_content_value(record.content or "", "model_hint") or "").strip(),
        "core_instinct_ids": list((record.meta or {}).get("core_instinct_ids") or _extract_content_csv(record.content or "", "core_instinct_ids")),
        "supporting_instinct_ids": list((record.meta or {}).get("supporting_instinct_ids") or _extract_content_csv(record.content or "", "supporting_instinct_ids")),
        "project_context_summary": str((record.meta or {}).get("project_context_summary") or _extract_content_value(record.content or "", "project_context_summary") or "").strip(),
        "project_context_refs": dict((record.meta or {}).get("project_context_refs") or {}),
        "project_context_snapshot": str((record.meta or {}).get("project_context_snapshot") or _extract_content_multiline(record.content or "", "project_context_snapshot") or "").strip(),
    }
    routing = await _route_task_packet_execution(
        qdrant=qdrant,
        body=RouteTaskPacketExecutionRequest(memory_id=str(record.id)),
    )
    recommended_executor = str(routing.get("recommended_executor") or "")
    recommended_model = str(routing.get("recommended_model") or "").strip() or None
    if recommended_executor not in {"local_slm_background", "background_llm", "cheap_subagent"}:
        raise HTTPException(status_code=400, detail="Packet is not eligible for background dispatch")

    job_type = str(packet.get("background_job_type") or "").strip()
    payload = dict(packet.get("background_payload") or {})
    if not job_type:
        job_type = "handoff_packet_llm"
        payload = {
            "memory_id": str(record.id),
            "task_description": str(packet.get("task_description") or "").strip(),
            "write_scope": list(packet.get("write_scope") or []),
            "phase": str(packet.get("phase") or "").strip(),
            "priority": str(packet.get("priority") or "").strip(),
            "why_now": str(packet.get("why_now") or "").strip(),
            "definition_of_done": str(packet.get("definition_of_done") or "").strip(),
            "expected_output_shape": str(packet.get("expected_output_shape") or "").strip(),
            "phase_objective": str(packet.get("phase_objective") or "").strip(),
            "execution_mode": str(packet.get("execution_mode") or "").strip(),
            "task_type": str((routing.get("routing_basis") or {}).get("task_type") or "").strip(),
            "recommended_executor": recommended_executor,
            "recommended_model": recommended_model,
            "routing_basis": dict(routing.get("routing_basis") or {}),
            "recommendation_reason": str(routing.get("recommendation_reason") or "").strip(),
            "core_instinct_ids": list(packet.get("core_instinct_ids") or []),
            "supporting_instinct_ids": list(packet.get("supporting_instinct_ids") or []),
            "project_context_summary": str(packet.get("project_context_summary") or "").strip(),
            "project_context_refs": dict(packet.get("project_context_refs") or {}),
            "project_context_snapshot": str(packet.get("project_context_snapshot") or "").strip(),
        }
    else:
        if job_type not in _SUPPORTED_PACKET_BACKGROUND_JOB_TYPES:
            raise HTTPException(status_code=400, detail="Packet does not declare a supported background_job_type")
        if not payload:
            raise HTTPException(status_code=400, detail="Packet does not declare a background_payload")

    from app.services.job_queue import get_job_queue

    queue = get_job_queue()
    job_id = await queue.submit(job_type, payload)
    meta = dict(record.meta or {})
    meta["handoff_status_updated_by"] = body.acted_by
    meta["handoff_status_reason"] = body.reason
    meta["executor_used"] = recommended_executor
    if recommended_model:
        meta["model_used"] = recommended_model
    meta["dispatched_job_id"] = job_id
    content = _upsert_handoff_content_fields(
        record.content or "",
        {
            "executor_used": recommended_executor,
            "model_used": recommended_model,
        },
    )
    updated = await qdrant.update(
        packet_uuid,
        MemoryUpdate(status="active", meta=meta, content=content),
    )
    return {
        "memory_id": str(updated.id),
        "status": updated.status or "active",
        "executor_used": recommended_executor,
        "model_used": recommended_model,
        "background_job_type": job_type,
        "background_job_status": "queued",
        "dispatched_job_id": job_id,
        "job_id": job_id,
        "poll": f"/api/v1/tasks/{job_id}",
        "routing_basis": routing.get("routing_basis"),
        "recommendation_reason": routing.get("recommendation_reason"),
    }


def _summarize_background_job_result(value: Any) -> str:
    if isinstance(value, dict):
        preferred = (
            "summary",
            "verification",
            "deliverable",
            "message",
            "project",
            "generated_at",
            "memoir_id",
            "sections",
            "synced_doc_sections",
            "implementation_plan",
            "proposed_patch",
        )
        parts: list[str] = []
        for key in preferred:
            raw = value.get(key)
            if raw in (None, "", [], {}):
                continue
            if isinstance(raw, list):
                parts.append(f"{key}={len(raw)}")
            else:
                parts.append(f"{key}={str(raw)[:80]}")
            if len(parts) >= 3:
                break
        if parts:
            return "; ".join(parts)[:240]
        keys = [str(key) for key in value.keys()][:4]
        return f"result_keys={', '.join(keys)}"[:240]
    return str(value)[:240]


def _extract_background_job_model_used(job: dict[str, Any]) -> str | None:
    result = job.get("result")
    if isinstance(result, dict):
        for key in ("model_used", "model_id", "component"):
            raw = str(result.get(key) or "").strip()
            if raw:
                return raw[:128]
    return None


async def _reconcile_background_task_packet(
    *,
    qdrant,
    body: ReconcileBackgroundPacketRequest,
) -> dict[str, Any]:
    try:
        packet_uuid = UUID(str(body.memory_id))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid memory_id") from exc

    record = await qdrant.get(packet_uuid)
    if record.category != "handoff":
        raise HTTPException(status_code=404, detail="Handoff not found")

    meta = dict(record.meta or {})
    job_id = str(meta.get("dispatched_job_id") or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="Packet has no dispatched background job")

    from app.services.job_queue import get_job_queue

    job = get_job_queue().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Background job not found")

    job_status = str(job.get("status") or "").strip() or "unknown"
    meta["handoff_status_updated_by"] = body.acted_by
    meta["handoff_status_reason"] = body.reason
    meta["background_job_status"] = job_status

    new_status = str(record.status or "active")
    result_summary: str | None = None
    verification_summary: str | None = None
    model_used = _extract_background_job_model_used(job)
    if model_used:
        meta["model_used"] = model_used

    if job_status == "done":
        new_status = "closed"
        result_summary = f"Background job {job.get('job_type')} completed."
        verification_summary = _summarize_background_job_result(job.get("result"))
        meta["result_summary"] = result_summary
        meta["verification_summary"] = verification_summary
    elif job_status == "failed":
        new_status = "paused"
        verification_summary = f"Background job {job.get('job_type')} failed: {str(job.get('error') or 'unknown error')[:200]}"
        meta["verification_summary"] = verification_summary

    content = _upsert_handoff_content_fields(
        record.content or "",
        {
            "model_used": model_used,
            "result_summary": result_summary,
            "verification_summary": verification_summary,
        },
    )
    updated = await qdrant.update(
        packet_uuid,
        MemoryUpdate(status=new_status, meta=meta, content=content),
    )
    return {
        "memory_id": str(updated.id),
        "status": updated.status or new_status,
        "job_id": job_id,
        "background_job_status": job_status,
        "background_job_type": job.get("job_type"),
        "executor_used": str(meta.get("executor_used") or "").strip() or None,
        "model_used": str(meta.get("model_used") or "").strip() or None,
        "result_summary": str(meta.get("result_summary") or "").strip() or None,
        "verification_summary": str(meta.get("verification_summary") or "").strip() or None,
        "poll": f"/api/v1/tasks/{job_id}",
    }


async def reconcile_background_task_packets(
    *,
    qdrant,
    limit: int = 100,
    acted_by: str = "background_sync",
    reason: str = "background_sync",
) -> dict[str, Any]:
    items = await qdrant.list_background_handoffs(
        limit=limit,
        statuses=["active"],
        mark_integrity_on_fallback=False,
    )
    updated = 0
    closed = 0
    paused = 0
    skipped_missing_job = 0
    status_counts: dict[str, int] = {}
    packets: list[dict[str, Any]] = []
    for item in items:
        before_status = str(item.get("status") or "").strip()
        before_job_status = str(item.get("background_job_status") or "").strip()
        try:
            result = await _reconcile_background_task_packet(
                qdrant=qdrant,
                body=ReconcileBackgroundPacketRequest(
                    memory_id=str(item["memory_id"]),
                    acted_by=acted_by,
                    reason=reason,
                ),
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                skipped_missing_job += 1
                continue
            raise
        after_status = str(result.get("status") or "").strip()
        after_job_status = str(result.get("background_job_status") or "").strip()
        if after_status != before_status or after_job_status != before_job_status:
            updated += 1
        if after_status == "closed":
            closed += 1
        elif after_status == "paused":
            paused += 1
        if after_job_status:
            status_counts[after_job_status] = status_counts.get(after_job_status, 0) + 1
        packets.append(
            {
                "memory_id": result.get("memory_id"),
                "status": after_status,
                "background_job_status": after_job_status,
                "job_id": result.get("job_id"),
                "executor_used": result.get("executor_used"),
                "model_used": result.get("model_used"),
            }
        )
    return {
        "scanned": len(items),
        "updated": updated,
        "closed": closed,
        "paused": paused,
        "skipped_missing_job": skipped_missing_job,
        "by_background_job_status": status_counts,
        "packets": packets,
    }


def _upsert_handoff_content_fields(content: str, updates: dict[str, str | None]) -> str:
    lines = (content or "").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        replaced = False
        for key, value in list(remaining.items()):
            prefix = f"{key}:"
            if line.startswith(prefix):
                if value:
                    out.append(f"{key}: {value}")
                remaining.pop(key, None)
                replaced = True
                break
        if not replaced:
            out.append(line)
    for key, value in remaining.items():
        if value:
            out.append(f"{key}: {value}")
    return "\n".join(out)


def _summarize_handoff_ref_counts(refs: dict[str, list[str]]) -> dict[str, int]:
    return {
        str(key): len([str(item).strip() for item in values if str(item).strip()])
        for key, values in (refs or {}).items()
        if isinstance(values, list) and any(str(item).strip() for item in values)
    }


def _handoff_capture_signal(item: dict[str, Any]) -> tuple[int, bool]:
    refs = item.get("project_context_refs") or {}
    values = refs.get("task_capture_candidates") or []
    count = len([str(value).strip() for value in values if str(value).strip()]) if isinstance(values, list) else 0
    return count, count > 0


def _augment_handoff_with_capture_signal(item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    count, incomplete = _handoff_capture_signal(item)
    enriched["task_capture_candidate_count"] = count
    enriched["task_statement_incomplete"] = incomplete
    return enriched


def _handoff_project_id(item: dict[str, Any]) -> str:
    project_id = str(item.get("project_id") or "").strip()
    if project_id:
        return project_id
    meta = item.get("meta") or {}
    project_id = str(meta.get("project_id") or "").strip()
    if project_id:
        return project_id
    return _extract_content_value(str(item.get("content") or ""), "project_id")


async def _build_project_task_recommendations(
    *,
    qdrant,
    items: list[dict[str, Any]],
    limit_per_project: int = 3,
    max_projects: int = 5,
) -> dict[str, dict[str, Any]]:
    project_ids: list[str] = []
    for item in items:
        project_id = _handoff_project_id(item)
        if project_id and project_id not in project_ids:
            project_ids.append(project_id)
        if len(project_ids) >= max_projects:
            break
    if not project_ids:
        return {}
    triage_rows = await asyncio.gather(
        *(build_task_triage(project_id, qdrant, limit=limit_per_project) for project_id in project_ids)
    )
    recommendations: dict[str, dict[str, Any]] = {}
    for row in triage_rows:
        project_id = str((row or {}).get("project_id") or "").strip()
        if project_id:
            recommendations[project_id] = row
    return recommendations


def _augment_handoff_with_project_recommendation(
    item: dict[str, Any],
    recommendations: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    enriched = dict(item)
    project_id = _handoff_project_id(item)
    recommendation = (recommendations or {}).get(project_id) or {}
    enriched["project_recommended_task_id"] = str(recommendation.get("recommended_task_id") or "").strip()
    return enriched


def _sanitize_handoff_content_preview(content: str) -> str:
    filtered: list[str] = []
    skip_prefixes = (
        "project_context_summary:",
        "project_context_refs:",
        "project_context_snapshot:",
        "project_id:",
        "owner_agent:",
        "write_scope:",
        "executor_used:",
        "model_used:",
        "result_summary:",
        "verification_summary:",
        "phase:",
        "priority:",
        "definition_of_done:",
        "expected_output_shape:",
        "phase_objective:",
        "execution_mode:",
        "background_job_type:",
        "background_payload:",
        "background_job_status:",
        "dispatched_job_id:",
        "suggested_execution_tier:",
        "model_hint:",
        "core_instinct_ids:",
        "supporting_instinct_ids:",
    )
    for line in (content or "").splitlines():
        if any(line.startswith(prefix) for prefix in skip_prefixes):
            continue
        filtered.append(line)
    return "\n".join(filtered)[:400]


def _compact_handoff_item(
    item: dict[str, Any],
    *,
    project_task_recommendations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    refs = item.get("project_context_refs") or {}
    task_capture_candidate_count, task_statement_incomplete = _handoff_capture_signal(item)
    project_id = _handoff_project_id(item)
    project_recommended_task_id = str(
        ((project_task_recommendations or {}).get(project_id) or {}).get("recommended_task_id") or ""
    ).strip()
    compact = {
        "memory_id": item.get("memory_id"),
        "status": item.get("status"),
        "timestamp": item.get("timestamp"),
        "from_agent": item.get("from_agent"),
        "to_agent": item.get("to_agent"),
        "task_id": item.get("task_id"),
        "handoff_label": item.get("handoff_label"),
        "project_id": item.get("project_id"),
        "owner_agent": item.get("owner_agent"),
        "write_scope": item.get("write_scope") or [],
        "executor_used": item.get("executor_used") or "",
        "model_used": item.get("model_used") or "",
        "result_summary": item.get("result_summary") or "",
        "verification_summary": item.get("verification_summary") or "",
        "phase": item.get("phase"),
        "priority": item.get("priority"),
        "why_now": item.get("why_now"),
        "definition_of_done": item.get("definition_of_done"),
        "expected_output_shape": item.get("expected_output_shape"),
        "phase_objective": item.get("phase_objective"),
        "execution_mode": item.get("execution_mode"),
        "background_job_type": item.get("background_job_type"),
        "background_job_status": item.get("background_job_status"),
        "dispatched_job_id": item.get("dispatched_job_id"),
        "suggested_execution_tier": item.get("suggested_execution_tier"),
        "model_hint": item.get("model_hint"),
        "core_instinct_ids": item.get("core_instinct_ids") or [],
        "supporting_instinct_ids": item.get("supporting_instinct_ids") or [],
        "project_context_summary": item.get("project_context_summary") or "",
        "project_context_ref_counts": _summarize_handoff_ref_counts(refs),
        "task_capture_candidate_count": task_capture_candidate_count,
        "task_statement_incomplete": task_statement_incomplete,
        "project_recommended_task_id": project_recommended_task_id,
        "content_preview": _sanitize_handoff_content_preview(str(item.get("content") or "")),
    }
    return compact


def _handoff_attention_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
    capture_count = item.get("task_capture_candidate_count")
    incomplete = item.get("task_statement_incomplete")
    if capture_count in (None, "") or incomplete is None:
        capture_count, incomplete = _handoff_capture_signal(item)
    return (
        0 if bool(incomplete) else 1,
        -int(capture_count or 0),
        _packet_priority_rank(item),
        str(item.get("timestamp") or ""),
    )


def _packet_write_scope_set(item: dict[str, Any]) -> set[str]:
    return {scope for scope in _normalize_write_scope(item.get("write_scope") or []) if scope}


def _packet_priority_rank(item: dict[str, Any]) -> int:
    priority = str(item.get("priority") or "").strip().lower()
    ranking = {
        "critical": 0,
        "urgent": 1,
        "high": 2,
        "medium": 3,
        "low": 4,
    }
    return ranking.get(priority, 5)


def _build_parallel_execution_summary(
    items: list[dict[str, Any]],
    *,
    project_task_recommendations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    running_packets = [item for item in items if str(item.get("status") or "").strip() == "active"]
    candidate_packets = [
        item
        for item in items
        if str(item.get("status") or "").strip() in {"pending", "picked_up", "paused"}
    ]
    candidate_packets.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    candidate_packets.sort(key=_packet_priority_rank)

    active_scope_to_packets: dict[str, set[str]] = {}
    for packet in running_packets:
        memory_id = str(packet.get("memory_id") or "").strip()
        for scope in _packet_write_scope_set(packet):
            active_scope_to_packets.setdefault(scope, set()).add(memory_id)

    waves: list[dict[str, Any]] = []
    blocked_packets: list[dict[str, Any]] = []
    for packet in candidate_packets:
        packet_scopes = _packet_write_scope_set(packet)
        conflict_scopes = sorted(scope for scope in packet_scopes if scope in active_scope_to_packets)
        if conflict_scopes:
            conflict_ids: set[str] = set()
            for scope in conflict_scopes:
                conflict_ids.update(active_scope_to_packets.get(scope) or set())
            blocked_packets.append(
                {
                    "packet": _compact_handoff_item(
                        packet,
                        project_task_recommendations=project_task_recommendations,
                    ),
                    "reason": "write_scope_conflict_with_active_packet",
                    "conflict_scopes": conflict_scopes,
                    "conflicts_with": sorted(conflict_ids),
                }
            )
            continue

        placed = False
        for wave in waves:
            wave_scopes = wave["write_scope_set"]
            if packet_scopes and wave_scopes and not packet_scopes.isdisjoint(wave_scopes):
                continue
            wave["packets"].append(
                _compact_handoff_item(
                    packet,
                    project_task_recommendations=project_task_recommendations,
                )
            )
            wave_scopes.update(packet_scopes)
            placed = True
            break
        if not placed:
            waves.append(
                {
                    "packets": [
                        _compact_handoff_item(
                            packet,
                            project_task_recommendations=project_task_recommendations,
                        )
                    ],
                    "write_scope_set": set(packet_scopes),
                }
            )

    finalized_waves: list[dict[str, Any]] = []
    for index, wave in enumerate(waves, start=1):
        finalized_waves.append(
            {
                "wave": index,
                "packet_count": len(wave["packets"]),
                "write_scope_union": sorted(wave["write_scope_set"]),
                "packets": wave["packets"],
            }
        )

    return {
        "running_count": len(running_packets),
        "planned_packet_count": sum(item["packet_count"] for item in finalized_waves),
        "blocked_count": len(blocked_packets),
        "running_packets": _compact_handoff_items_with_recommendations(
            running_packets,
            project_task_recommendations=project_task_recommendations,
        ),
        "waves": finalized_waves,
        "blocked_packets": blocked_packets,
        "guidance": [
            "Run packets from the same wave in parallel; run later waves after the previous wave merges cleanly.",
            "Blocked packets conflict with currently active write_scope and should wait for merge-back or ownership transfer.",
            "Keep write_scope explicit so the planner can continue proposing safe parallel waves.",
        ],
    }


def _compact_handoff_items_with_recommendations(
    items: list[dict[str, Any]],
    *,
    project_task_recommendations: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        _compact_handoff_item(item, project_task_recommendations=project_task_recommendations)
        for item in items
    ]


def _build_handoff_merge_back_guidance(
    *,
    total: int,
    by_status: dict[str, int],
) -> dict[str, Any]:
    active = int(by_status.get("active", 0))
    paused = int(by_status.get("paused", 0))
    closed = int(by_status.get("closed", 0))
    if active > 0:
        recommended = "Verify bounded results on active packets, record result_summary and verification_summary, then close them."
    elif paused > 0:
        recommended = "Resume paused packets that are ready to continue, refresh context if needed, and finish verification before closure."
    elif closed > 0:
        recommended = "Review closed packets for reusable outcomes and follow-up splits if additional merge-back work remains."
    elif total > 0:
        recommended = "Inspect recent packets and move them into an explicit active, paused, or closed state before merging work back."
    else:
        recommended = "No packet merge-back work is pending."
    return {
        "recommended_next_step": recommended,
        "steps": [
            "Verify the bounded result before trusting completion claims.",
            "Record a short result_summary and verification_summary on packet closure.",
            "Close the packet only after the bounded result is merged back into the main thread.",
            "Resume or refresh dependent packets when their context changed because of the merged result.",
        ],
    }


async def _refresh_handoff_context_record(
    *,
    qdrant,
    ollama,
    record,
    task_description: str | None,
    max_components: int,
    status_override: str | None = None,
    meta_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(record.meta or {})
    project_id = str(meta.get("project_id") or record.project or "").strip() or _extract_content_value(record.content or "", "project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="Handoff has no project_id; context refresh is unavailable")
    resolved_task = (task_description or _extract_content_value(record.content or "", "task") or "").strip()
    if not resolved_task:
        raise HTTPException(status_code=400, detail="Handoff has no task description to refresh against")

    bundle = await assemble_project_context(
        project_id=project_id,
        task=resolved_task,
        qdrant=qdrant,
        ollama=ollama,
        context_profile="handoff_compact",
        max_components=max_components,
    )
    enrich_data = {
        "laws": bundle.laws,
        "components": bundle.components,
        "improvements": bundle.improvements,
        "runtime_hints": bundle.runtime_hints,
        "tasks": bundle.tasks,
        "task_triage": bundle.task_triage,
        "task_capture_candidates": bundle.task_capture_candidates,
        "docs_sections": bundle.docs_sections,
        "code_inspection_recommended": bundle.code_inspection_recommended,
    }
    summary = _build_handoff_context_summary(enrich_data)
    refs = _build_handoff_context_refs(enrich_data)
    meta["project_id"] = project_id
    meta["project_context_summary"] = summary
    meta["project_context_refs"] = refs
    meta["project_context_refreshed_task"] = resolved_task
    if meta_updates:
        meta.update(meta_updates)
    content = _upsert_handoff_content_fields(
        record.content or "",
        {
            "owner_agent": str(meta.get("owner_agent") or "").strip()[:128] or None,
            "write_scope": ", ".join(_normalize_write_scope(meta.get("write_scope") or [])) or None,
        },
    )
    update = MemoryUpdate(meta=meta, content=content)
    if status_override:
        update.status = status_override
    updated = await qdrant.update(record.id, update)
    return {
        "memory_id": str(updated.id),
        "status": updated.status or status_override or record.status or "unknown",
        "project_id": project_id,
        "task_description": resolved_task,
        "owner_agent": str(meta.get("owner_agent") or "").strip() or None,
        "write_scope": _normalize_write_scope(meta.get("write_scope") or []),
        "project_context_summary": summary,
        "project_context_refs": refs,
        "code_inspection_recommended": bundle.code_inspection_recommended,
        "coverage": bundle.coverage,
    }


def _build_handoff_context_refs(enrich_data: dict[str, Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    mapping = (
        ("laws", "id"),
        ("components", "component_id"),
        ("improvements", "id"),
        ("runtime_hints", "id"),
        ("tasks", "task_id"),
        ("task_capture_candidates", "artifact_id"),
        ("docs_sections", "section_key"),
    )
    for key, field in mapping:
        values = [
            str(item.get(field) or "").strip()
            for item in enrich_data.get(key) or []
            if isinstance(item, dict) and str(item.get(field) or "").strip()
        ]
        if values:
            refs[key] = values[:10]
    return refs


def _build_handoff_context_summary(enrich_data: dict[str, Any]) -> str:
    coverage = []
    for key in ("laws", "components", "improvements", "runtime_hints", "tasks", "task_capture_candidates", "docs_sections"):
        count = len(enrich_data.get(key) or [])
        if count:
            coverage.append(f"{key}={count}")
    highlights: list[str] = []
    laws = enrich_data.get("laws") or []
    if laws:
        titles = [str(item.get("title") or "").strip() for item in laws[:2] if str(item.get("title") or "").strip()]
        if titles:
            highlights.append("laws: " + ", ".join(titles))
    components = enrich_data.get("components") or []
    if components:
        names = [
            str(item.get("name") or item.get("component_id") or "").strip()
            for item in components[:2]
            if str(item.get("name") or item.get("component_id") or "").strip()
        ]
        if names:
            highlights.append("components: " + ", ".join(names))
    improvements = enrich_data.get("improvements") or []
    if improvements:
        titles = [str(item.get("title") or "").strip() for item in improvements[:2] if str(item.get("title") or "").strip()]
        if titles:
            highlights.append("improvements: " + ", ".join(titles))
    task_triage = enrich_data.get("task_triage") or {}
    recommended_task_id = str(task_triage.get("recommended_task_id") or "").strip()
    if recommended_task_id:
        highlights.append("next_task: " + recommended_task_id)
    task_capture_candidates = enrich_data.get("task_capture_candidates") or []
    if task_capture_candidates:
        labels = []
        for item in task_capture_candidates[:2]:
            kind = str(item.get("kind") or "draft").strip()
            task_id = str(item.get("task_id") or "").strip()
            labels.append(f"{kind}@{task_id}" if task_id else kind)
        if labels:
            highlights.append("capture_drafts: " + ", ".join(labels))
    parts: list[str] = []
    if coverage:
        parts.append("coverage " + ", ".join(coverage))
    if highlights:
        parts.append("highlights " + " | ".join(highlights))
    if enrich_data.get("code_inspection_recommended"):
        parts.append("code inspection fallback recommended")
    return "; ".join(parts)[:2000]


async def _expand_handoff_refs_for_record(
    *,
    qdrant,
    handoff_record,
    ref_types: list[str],
    limit_per_type: int,
) -> dict[str, Any]:
    meta = dict(handoff_record.meta or {})
    refs = {
        str(key): [str(item).strip() for item in values if str(item).strip()]
        for key, values in (meta.get("project_context_refs") or {}).items()
        if isinstance(values, list)
    }
    project_id = str(meta.get("project_id") or "").strip() or _extract_content_value(handoff_record.content or "", "project_id")
    requested = _normalize_ref_types(ref_types) or [key for key in _SUPPORTED_HANDOFF_REF_TYPES if refs.get(key)]
    resolved: dict[str, list[dict[str, Any]]] = {}
    unresolved: dict[str, list[str]] = {}

    async def _resolve_laws(values: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for law_id in values[:limit_per_type]:
            try:
                record = await get_project_law(qdrant, law_id)
            except Exception:
                unresolved.setdefault("laws", []).append(law_id)
                continue
            items.append({
                "id": record.id,
                "title": record.title,
                "status": record.status,
                "statement": record.statement[:240],
            })
        return items

    async def _resolve_components(values: list[str]) -> list[dict[str, Any]]:
        if not project_id:
            unresolved.setdefault("components", []).extend(values[:limit_per_type])
            return []
        svc = ProjectKnowledgeService(qdrant._client, None)  # type: ignore[arg-type]
        items: list[dict[str, Any]] = []
        for component_id in values[:limit_per_type]:
            try:
                payload = await svc.get_component(project_id, component_id)
            except Exception:
                payload = None
            if not payload:
                unresolved.setdefault("components", []).append(component_id)
                continue
            items.append({
                "component_id": str(payload.get("component_id") or component_id),
                "name": str(payload.get("name") or payload.get("component_id") or component_id),
                "summary": str(
                    payload.get("summary")
                    or payload.get("purpose")
                    or payload.get("implementation")
                    or ""
                )[:240],
            })
        return items

    async def _resolve_improvements(values: list[str]) -> list[dict[str, Any]]:
        store = get_improvements_store()
        items: list[dict[str, Any]] = []
        for improvement_id in values[:limit_per_type]:
            try:
                row = await store.get(UUID(improvement_id))
            except Exception:
                row = None
            if not row:
                unresolved.setdefault("improvements", []).append(improvement_id)
                continue
            items.append({
                "id": str(row.get("id") or improvement_id),
                "title": str(row.get("title") or improvement_id),
                "status": str(row.get("status") or "unknown"),
                "description": str(row.get("description") or "")[:240],
            })
        return items

    async def _resolve_runtime_hints(values: list[str]) -> list[dict[str, Any]]:
        store = get_learning_store()
        items: list[dict[str, Any]] = []
        for artifact_id in values[:limit_per_type]:
            try:
                row = await store.get_artifact(UUID(artifact_id))
            except Exception:
                row = None
            if not row:
                unresolved.setdefault("runtime_hints", []).append(artifact_id)
                continue
            items.append({
                "id": str(row.get("id") or artifact_id),
                "action_type": str(row.get("action_type") or row.get("artifact_type") or "runtime_hint"),
                "status": str(row.get("status") or "active"),
                "content": str(row.get("content") or "")[:240],
            })
        return items

    async def _resolve_tasks(values: list[str]) -> list[dict[str, Any]]:
        if not project_id:
            unresolved.setdefault("tasks", []).extend(values[:limit_per_type])
            return []
        items: list[dict[str, Any]] = []
        for task_id in values[:limit_per_type]:
            try:
                row = await get_project_task(qdrant, project=project_id, task_id=task_id, include_changes=False)
            except Exception:
                row = None
            if not row:
                unresolved.setdefault("tasks", []).append(task_id)
                continue
            items.append({
                "task_id": row.task_id,
                "title": row.title,
                "status": row.status,
                "description": row.description[:240],
            })
        return items

    async def _resolve_task_capture_candidates(values: list[str]) -> list[dict[str, Any]]:
        store = get_learning_store()
        items: list[dict[str, Any]] = []
        for artifact_id in values[:limit_per_type]:
            try:
                row = await store.get_artifact(UUID(artifact_id))
            except Exception:
                row = None
            if not row:
                unresolved.setdefault("task_capture_candidates", []).append(artifact_id)
                continue
            tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
            items.append({
                "artifact_id": str(row.get("id") or artifact_id),
                "task_id": next((tag.split(":", 1)[1] for tag in tags if tag.startswith("task_id:")), ""),
                "kind": next((tag.split(":", 1)[1] for tag in tags if tag.startswith("capture_kind:")), "draft"),
                "source": next((tag.split(":", 1)[1] for tag in tags if tag.startswith("capture_source:")), ""),
                "status": str(row.get("status") or "active"),
                "content": str(row.get("content") or "")[:240],
            })
        return items

    async def _resolve_docs_sections(values: list[str]) -> list[dict[str, Any]]:
        if not project_id:
            unresolved.setdefault("docs_sections", []).extend(values[:limit_per_type])
            return []
        status = load_docs_cache(project_id)
        if not status:
            unresolved.setdefault("docs_sections", []).extend(values[:limit_per_type])
            return []
        items: list[dict[str, Any]] = []
        effective = dict(getattr(status, "sections", {}) or {})
        candidate = dict(getattr(status, "candidate_sections", {}) or {})
        for section_key in values[:limit_per_type]:
            section = candidate.get(section_key) or effective.get(section_key)
            if not section:
                unresolved.setdefault("docs_sections", []).append(section_key)
                continue
            items.append({
                "section_key": section_key,
                "name": str(getattr(section, "name", "") or section_key),
                "content_preview": str(getattr(section, "content", "") or "")[:240],
            })
        return items

    resolvers = {
        "laws": _resolve_laws,
        "components": _resolve_components,
        "improvements": _resolve_improvements,
        "runtime_hints": _resolve_runtime_hints,
        "tasks": _resolve_tasks,
        "task_capture_candidates": _resolve_task_capture_candidates,
        "docs_sections": _resolve_docs_sections,
    }
    for ref_type in requested:
        values = refs.get(ref_type) or []
        if not values:
            continue
        items = await resolvers[ref_type](values)
        if items:
            resolved[ref_type] = items

    return {
        "memory_id": str(handoff_record.id),
        "project_id": project_id or None,
        "available_ref_types": [key for key in _SUPPORTED_HANDOFF_REF_TYPES if refs.get(key)],
        "requested_ref_types": requested,
        "resolved": resolved,
        "unresolved": {key: values for key, values in unresolved.items() if values},
    }


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/register")
async def register_model(body: RegisterRequest):
    """Register or update a cloud model with quota config."""
    reg = get_model_registry()
    quota = reg.register(
        model_id=body.model_id,
        display_name=body.display_name,
        provider=body.provider,
        daily_limit=body.daily_limit,
        limit_unit=body.limit_unit,
        priority=body.priority,
        task_capabilities=body.task_capabilities,
        initial_scores=body.initial_scores,
        weekly_limit=body.weekly_limit,
    )
    return {"registered": True, "model_id": quota.model_id, "daily_limit": quota.daily_limit}


@router.get("/available")
async def list_available(task_type: Optional[str] = None):
    """List available models ranked by priority. Filter by task_type capability."""
    reg = get_model_registry()
    quotas = reg.available(task_type=task_type)
    return [
        {
            "model_id": q.model_id,
            "display_name": q.display_name,
            "provider": q.provider,
            "remaining": q.remaining,
            "remaining_pct": round(q.remaining_fraction * 100, 1),
            "limit_unit": q.limit_unit,
            "priority": q.priority,
            "is_available": q.is_available,
            "task_capabilities": q.task_capabilities,
        }
        for q in quotas
        if q.is_available
    ]


@router.get("/status")
async def quota_status():
    """Quota dashboard for all registered models."""
    return get_model_registry().status_dashboard()


@router.get("/handoff_log")
async def get_handoff_log(
    limit: int = 20,
    handoff_label: Optional[str] = Query(None, max_length=64),
):
    """Recent cross-CLI handoff events."""
    return get_model_registry().handoff_log(
        limit=limit,
        handoff_label=_normalize_handoff_label(handoff_label),
    )


@router.post("/report_usage")
async def report_usage(body: ReportUsageRequest):
    """Record units consumed. Updates remaining quota."""
    reg = get_model_registry()
    if body.model_id not in reg._models:
        raise HTTPException(404, f"Model '{body.model_id}' not registered")
    quota = reg.record_usage(body.model_id, body.units_used)
    return {
        "model_id": quota.model_id,
        "used_today": quota.used_today,
        "remaining": quota.remaining,
        "is_available": quota.is_available,
    }


@router.post("/report_limit")
async def report_limit(body: ReportLimitRequest):
    """Mark model as rate-limited / quota-exhausted. Triggers cooldown."""
    reg = get_model_registry()
    if body.model_id not in reg._models:
        raise HTTPException(404, f"Model '{body.model_id}' not registered")
    quota = reg.report_limit_hit(
        model_id=body.model_id,
        error_code=body.error_code,
        error_msg=body.error_msg,
        retry_after=body.retry_after,
    )
    return {
        "model_id": quota.model_id,
        "is_available": quota.is_available,
        "cooldown_until": quota.cooldown_until,
    }


@router.post("/handoff")
async def create_handoff(body: HandoffRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Package task context in supermemory for pickup by target CLI.

    Stores handoff packet content/metadata durably in SQLite and keeps a
    lightweight reference payload in Qdrant (category='handoff', status='pending').
    Returns memory_id + next available models.
    """
    return await _create_handoff_record(body=body, qdrant=qdrant, ollama=ollama)


@router.post("/handoff/pickup")
async def pickup_handoff(body: PickupRequest, qdrant: QdrantDep):
    """
    Retrieve pending handoffs addressed to this agent/CLI.
    Updates their status to 'picked_up' to prevent double-pickup.
    """
    handoff_label = _normalize_handoff_label(body.handoff_label)
    handoffs = await qdrant.get_pending_handoffs(
        to_agent=body.agent_id,
        limit=body.limit,
        handoff_label=handoff_label,
    )
    raw_results = []
    for h in handoffs:
        await qdrant.mark_handoff_picked_up(h["memory_id"])
        raw_results.append(_augment_handoff_with_capture_signal(h))
    project_task_recommendations = await _build_project_task_recommendations(qdrant=qdrant, items=raw_results)
    results = [
        _augment_handoff_with_project_recommendation(item, project_task_recommendations)
        for item in raw_results
    ]
    results.sort(key=_handoff_attention_sort_key)
    return {
        "agent_id": body.agent_id,
        "handoff_label": handoff_label,
        "found": len(results),
        "project_task_recommendations": project_task_recommendations,
        "handoffs": results,
    }


@router.post("/handoff/list")
async def list_handoffs(body: ListHandoffsRequest, qdrant: QdrantDep):
    handoff_label = _normalize_handoff_label(body.handoff_label)
    statuses = _normalize_handoff_statuses(body.statuses)
    items = await qdrant.list_handoffs(
        to_agent=body.agent_id,
        limit=body.limit,
        handoff_label=handoff_label,
        statuses=statuses,
        owner_agent=(body.owner_agent or "").strip() or None,
        write_scope=_normalize_write_scope(body.write_scope),
    )
    project_task_recommendations = await _build_project_task_recommendations(qdrant=qdrant, items=items)
    result_items = (
        _compact_handoff_items_with_recommendations(
            items,
            project_task_recommendations=project_task_recommendations,
        )
        if body.compact
        else [_augment_handoff_with_project_recommendation(item, project_task_recommendations) for item in items]
    )
    result_items.sort(key=_handoff_attention_sort_key)
    return {
        "agent_id": body.agent_id,
        "handoff_label": handoff_label,
        "statuses": statuses or ["all"],
        "compact": body.compact,
        "found": len(result_items),
        "project_task_recommendations": project_task_recommendations,
        "handoffs": result_items,
    }


@router.post("/handoff/workspace_summary")
async def handoff_workspace_summary(body: HandoffWorkspaceSummaryRequest, qdrant: QdrantDep):
    handoff_label = _normalize_handoff_label(body.handoff_label)
    statuses = _normalize_handoff_statuses(body.statuses)
    owner_agent = (body.owner_agent or "").strip() or None
    write_scope = _normalize_write_scope(body.write_scope)
    items = await qdrant.list_handoffs(
        to_agent=body.agent_id,
        limit=200,
        handoff_label=handoff_label,
        statuses=statuses,
        owner_agent=owner_agent,
        write_scope=write_scope,
    )
    project_task_recommendations = await _build_project_task_recommendations(qdrant=qdrant, items=items)
    by_status: dict[str, int] = {}
    by_owner_agent: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    by_execution_mode: dict[str, int] = {}
    by_executor_used: dict[str, int] = {}
    task_statement_incomplete_count = 0
    by_task_capture_candidate_count: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        owner = str(item.get("owner_agent") or "").strip() or "unassigned"
        by_owner_agent[owner] = by_owner_agent.get(owner, 0) + 1
        phase = str(item.get("phase") or "").strip() or "unspecified"
        by_phase[phase] = by_phase.get(phase, 0) + 1
        execution_mode = str(item.get("execution_mode") or "").strip() or "unspecified"
        by_execution_mode[execution_mode] = by_execution_mode.get(execution_mode, 0) + 1
        executor_used = str(item.get("executor_used") or "").strip()
        if executor_used:
            by_executor_used[executor_used] = by_executor_used.get(executor_used, 0) + 1
        capture_count, incomplete = _handoff_capture_signal(item)
        if incomplete:
            task_statement_incomplete_count += 1
        capture_bucket = str(capture_count)
        by_task_capture_candidate_count[capture_bucket] = by_task_capture_candidate_count.get(capture_bucket, 0) + 1
    prioritized_items = sorted(
        _compact_handoff_items_with_recommendations(
            items,
            project_task_recommendations=project_task_recommendations,
        ),
        key=_handoff_attention_sort_key,
    )
    recent_packets = prioritized_items[: body.packet_limit]
    pending_labels = await qdrant.list_pending_handoff_labels(to_agent=body.agent_id, limit=10)
    merge_back_guidance = _build_handoff_merge_back_guidance(total=len(items), by_status=by_status)
    parallel_execution = _build_parallel_execution_summary(
        items,
        project_task_recommendations=project_task_recommendations,
    )
    return {
        "agent_id": body.agent_id,
        "handoff_label": handoff_label,
        "statuses": statuses or ["all"],
        "owner_agent": owner_agent,
        "write_scope": write_scope,
        "total": len(items),
        "by_status": by_status,
        "by_owner_agent": by_owner_agent,
        "by_phase": by_phase,
        "by_execution_mode": by_execution_mode,
        "by_executor_used": by_executor_used,
        "task_statement_incomplete_count": task_statement_incomplete_count,
        "by_task_capture_candidate_count": by_task_capture_candidate_count,
        "project_task_recommendations": project_task_recommendations,
        "merge_back_guidance": merge_back_guidance,
        "parallel_execution": parallel_execution,
        "pending_labels": pending_labels,
        "recent_packets": recent_packets,
    }


@router.post("/handoff/decompose")
async def decompose_task_packet(body: DecomposeTaskPacketRequest):
    write_scope = _normalize_write_scope(body.write_scope)
    execution_mode = _normalize_execution_mode(body.execution_mode)
    playbook = build_operational_instinct_playbook(
        family="task_lifecycle",
        project_id=(body.project_id or "").strip() or None,
    )
    available_phases = {item["phase"]: item for item in playbook.get("phases") or []}
    requested_phase = str(body.phase or "").strip()
    if requested_phase and requested_phase in available_phases:
        phase = requested_phase
    else:
        phase = "pre_implementation" if write_scope else "task_framing"
    phase_data = available_phases.get(phase) or {}
    objective = str(phase_data.get("objective") or "").strip() or (
        "Turn the current task into a bounded packet that can be executed, paused, resumed, or delegated safely."
    )
    label_prefix = _slugify_handoff_label_prefix(body.handoff_label_prefix, fallback="packet")
    strategy = _build_packet_decomposition_strategy(write_scope=write_scope, max_packets=body.max_packets)
    grouped_scopes = _chunk_write_scopes(write_scope, body.max_packets) or [[]]
    packets = [
        _build_decomposition_packet_stub(
            label_prefix=label_prefix,
            phase=phase,
            priority=body.priority,
            owner_agent=(body.owner_agent or "").strip() or None,
            packet_index=index + 1,
            scopes=scopes,
            objective=objective,
            core_instinct_ids=list(phase_data.get("core_instinct_ids") or []),
            supporting_instinct_ids=list(phase_data.get("supporting_instinct_ids") or []),
            task_description=body.task_description,
            execution_mode=execution_mode,
        )
        for index, scopes in enumerate(grouped_scopes[: body.max_packets])
    ]
    split_guidance = [
        "Keep each packet narrow enough that ownership and write scope remain obvious.",
        "Prefer packets that can be verified and closed independently before merge-back.",
    ]
    if len(packets) > 1:
        split_guidance.append("Parallelize only packets that remain mergeable without manual context reconstruction.")
    return {
        "project_id": (body.project_id or "").strip() or None,
        "task_description": body.task_description,
        "strategy": strategy,
        "recommended_packet_count": len(packets),
        "why_split": _build_packet_decomposition_why_split(strategy=strategy, write_scope=write_scope),
        "phase": phase,
        "execution_mode": execution_mode,
        "phase_objective": objective,
        "available_phases": playbook.get("phase_sequence") or [],
        "split_guidance": split_guidance,
        "packets": packets,
    }


@router.post("/handoff/create_packets")
async def create_task_packets(body: CreateTaskPacketsRequest, qdrant: QdrantDep, ollama: OllamaDep):
    created: list[dict[str, Any]] = []
    fallback_execution_mode = _normalize_execution_mode(body.execution_mode)
    for packet in body.packets:
        handoff = HandoffRequest(
            from_agent=body.from_agent,
            to_agent=body.to_agent,
            project_id=body.project_id,
            owner_agent=packet.owner_agent,
            write_scope=packet.write_scope,
            phase=packet.phase,
            priority=packet.priority,
            why_now=packet.why_now,
            definition_of_done=packet.definition_of_done,
            expected_output_shape=packet.expected_output_shape,
            phase_objective=packet.phase_objective,
            execution_mode=packet.execution_mode or fallback_execution_mode,
            background_job_type=packet.background_job_type,
            background_payload=packet.background_payload,
            suggested_execution_tier=packet.suggested_execution_tier,
            model_hint=packet.model_hint,
            core_instinct_ids=packet.core_instinct_ids,
            supporting_instinct_ids=packet.supporting_instinct_ids,
            project_context_summary=packet.project_context_summary,
            project_context_refs=packet.project_context_refs,
            project_context_snapshot=packet.project_context_snapshot,
            from_model_id=body.from_model_id,
            task_description=(packet.task_description or body.task_description).strip(),
            partial_result=body.partial_result,
            key_facts=body.key_facts,
            handoff_label=packet.handoff_label,
            reason=body.reason,
            agent_id=body.agent_id,
        )
        created.append(await _create_handoff_record(body=handoff, qdrant=qdrant, ollama=ollama))
    return {
        "from_agent": body.from_agent,
        "to_agent": body.to_agent,
        "project_id": body.project_id,
        "created_count": len(created),
        "packets": created,
    }


@router.post("/handoff/route_execution")
async def route_task_packet_execution(body: RouteTaskPacketExecutionRequest, qdrant: QdrantDep):
    return await _route_task_packet_execution(qdrant=qdrant, body=body)


@router.post("/handoff/dispatch_background")
async def dispatch_background_task_packet(body: DispatchBackgroundPacketRequest, qdrant: QdrantDep):
    return await _dispatch_background_task_packet(qdrant=qdrant, body=body)


@router.post("/handoff/reconcile_background")
async def reconcile_background_task_packet(body: ReconcileBackgroundPacketRequest, qdrant: QdrantDep):
    return await _reconcile_background_task_packet(qdrant=qdrant, body=body)


@router.post("/handoff/status")
async def update_handoff_status(body: HandoffStatusUpdateRequest, qdrant: QdrantDep):
    try:
        memory_id = UUID(str(body.memory_id))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid memory_id") from exc
    record = await qdrant.get(memory_id)
    if record.category != "handoff":
        raise HTTPException(status_code=404, detail="Handoff not found")
    if not _HANDOFF_STATUS_PATTERN.fullmatch(body.status):
        raise HTTPException(status_code=400, detail="Invalid handoff status")
    meta = dict(record.meta or {})
    write_scope = _normalize_write_scope(body.write_scope)
    owner_agent = (body.owner_agent or str(meta.get("owner_agent") or _extract_content_value(record.content or "", "owner_agent") or "")).strip() or None
    meta["handoff_status_updated_by"] = body.acted_by
    meta["handoff_status_reason"] = body.reason
    if owner_agent:
        meta["owner_agent"] = owner_agent[:128]
    if write_scope:
        meta["write_scope"] = write_scope
    if body.executor_used:
        meta["executor_used"] = body.executor_used[:128]
    if body.model_used:
        meta["model_used"] = body.model_used[:128]
    if body.result_summary:
        meta["result_summary"] = body.result_summary[:1000]
    if body.verification_summary:
        meta["verification_summary"] = body.verification_summary[:1000]
    content = _upsert_handoff_content_fields(
        record.content or "",
        {
            "owner_agent": owner_agent[:128] if owner_agent else None,
            "write_scope": ", ".join(write_scope) if write_scope else None,
            "executor_used": body.executor_used[:128] if body.executor_used else None,
            "model_used": body.model_used[:128] if body.model_used else None,
            "result_summary": body.result_summary[:1000] if body.result_summary else None,
            "verification_summary": body.verification_summary[:1000] if body.verification_summary else None,
        },
    )
    updated = await qdrant.update(
        memory_id,
        MemoryUpdate(status=body.status, meta=meta, content=content),
    )
    return {
        "memory_id": str(updated.id),
        "status": updated.status or body.status,
        "acted_by": body.acted_by,
        "reason": body.reason,
        "owner_agent": owner_agent,
        "write_scope": write_scope or (meta.get("write_scope") or []),
        "executor_used": str(meta.get("executor_used") or body.executor_used or "").strip() or None,
        "model_used": str(meta.get("model_used") or body.model_used or "").strip() or None,
        "result_summary": str(meta.get("result_summary") or body.result_summary or "").strip() or None,
        "verification_summary": str(meta.get("verification_summary") or body.verification_summary or "").strip() or None,
    }


@router.post("/handoff/expand_refs")
async def expand_handoff_refs(body: ExpandHandoffRefsRequest, qdrant: QdrantDep):
    try:
        memory_id = UUID(str(body.memory_id))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid memory_id") from exc
    record = await qdrant.get(memory_id)
    if record.category != "handoff":
        raise HTTPException(status_code=404, detail="Handoff not found")
    return await _expand_handoff_refs_for_record(
        qdrant=qdrant,
        handoff_record=record,
        ref_types=body.ref_types,
        limit_per_type=body.limit_per_type,
    )


@router.post("/handoff/refresh_context")
async def refresh_handoff_context(body: RefreshHandoffContextRequest, qdrant: QdrantDep, ollama: OllamaDep):
    try:
        memory_id = UUID(str(body.memory_id))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid memory_id") from exc
    record = await qdrant.get(memory_id)
    if record.category != "handoff":
        raise HTTPException(status_code=404, detail="Handoff not found")
    return await _refresh_handoff_context_record(
        qdrant=qdrant,
        ollama=ollama,
        record=record,
        task_description=body.task_description,
        max_components=body.max_components,
    )


@router.post("/handoff/resume")
async def resume_handoff(body: ResumeHandoffRequest, qdrant: QdrantDep, ollama: OllamaDep):
    try:
        memory_id = UUID(str(body.memory_id))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid memory_id") from exc
    record = await qdrant.get(memory_id)
    if record.category != "handoff":
        raise HTTPException(status_code=404, detail="Handoff not found")

    meta_updates = {
        "handoff_status_updated_by": body.acted_by,
        "handoff_status_reason": body.reason,
    }
    write_scope = _normalize_write_scope(body.write_scope)
    owner_agent = (body.owner_agent or str((record.meta or {}).get("owner_agent") or _extract_content_value(record.content or "", "owner_agent") or "")).strip() or None
    if owner_agent:
        meta_updates["owner_agent"] = owner_agent[:128]
    if write_scope:
        meta_updates["write_scope"] = write_scope
    if body.refresh_context:
        try:
            refreshed = await _refresh_handoff_context_record(
                qdrant=qdrant,
                ollama=ollama,
                record=record,
                task_description=body.task_description,
                max_components=body.max_components,
                status_override="active",
                meta_updates=meta_updates,
            )
            refreshed["refreshed"] = True
            refreshed["acted_by"] = body.acted_by
            refreshed["reason"] = body.reason
            refreshed["phase"] = _extract_content_value(record.content or "", "phase") or None
            refreshed["priority"] = _extract_content_value(record.content or "", "priority") or None
            refreshed["owner_agent"] = owner_agent
            refreshed["write_scope"] = write_scope or ((record.meta or {}).get("write_scope") or _extract_content_csv(record.content or "", "write_scope"))
            refreshed["definition_of_done"] = _extract_content_value(record.content or "", "definition_of_done") or None
            refreshed["expected_output_shape"] = _extract_content_value(record.content or "", "expected_output_shape") or None
            refreshed["phase_objective"] = _extract_content_value(record.content or "", "phase_objective") or None
            return refreshed
        except HTTPException:
            pass

    meta = dict(record.meta or {})
    meta.update(meta_updates)
    content = _upsert_handoff_content_fields(
        record.content or "",
        {
            "owner_agent": owner_agent[:128] if owner_agent else None,
            "write_scope": ", ".join(write_scope) if write_scope else None,
        },
    )
    updated = await qdrant.update(memory_id, MemoryUpdate(status="active", meta=meta, content=content))
    return {
        "memory_id": str(updated.id),
        "status": updated.status or "active",
        "project_id": str(meta.get("project_id") or record.project or "").strip() or _extract_content_value(record.content or "", "project_id") or None,
        "task_description": (body.task_description or _extract_content_value(record.content or "", "task") or "").strip() or None,
        "phase": _extract_content_value(record.content or "", "phase") or None,
        "priority": _extract_content_value(record.content or "", "priority") or None,
        "owner_agent": owner_agent,
        "write_scope": write_scope or (meta.get("write_scope") or _extract_content_csv(record.content or "", "write_scope")),
        "definition_of_done": _extract_content_value(record.content or "", "definition_of_done") or None,
        "expected_output_shape": _extract_content_value(record.content or "", "expected_output_shape") or None,
        "phase_objective": _extract_content_value(record.content or "", "phase_objective") or None,
        "project_context_summary": str(meta.get("project_context_summary") or ""),
        "project_context_refs": meta.get("project_context_refs") or {},
        "refreshed": False,
        "acted_by": body.acted_by,
        "reason": body.reason,
    }


@router.get("/handoff/pending_labels")
async def list_pending_handoff_labels(
    qdrant: QdrantDep,
    agent_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(20, ge=1, le=100),
):
    labels = await qdrant.list_pending_handoff_labels(to_agent=agent_id, limit=limit)
    return {
        "agent_id": agent_id,
        "found": len(labels),
        "labels": labels,
    }


@router.post("/coordination/messages", response_model=CoordinationMessageRecord)
async def send_coordination_message(body: CoordinationMessageCreate, qdrant: QdrantDep, ollama: OllamaDep):
    return await create_coordination_message(qdrant, ollama, body)


@router.get("/coordination/messages", response_model=CoordinationListResponse)
async def list_coordination(
    qdrant: QdrantDep,
    agent_id: Optional[str] = Query(None, min_length=1, max_length=128),
    project: Optional[str] = Query(None, min_length=1, max_length=128),
    mailbox: str = Query("inbox", pattern=COORDINATION_MAILBOX_PATTERN),
    thread_id: Optional[str] = Query(None, min_length=1, max_length=128),
    status: Optional[str] = Query(None, pattern=COORDINATION_STATUS_PATTERN),
    limit: int = Query(20, ge=1, le=100),
):
    items = await list_coordination_messages(
        qdrant,
        agent_id=agent_id,
        project=project,
        mailbox=mailbox,
        thread_id=thread_id,
        status=status,
        limit=limit,
    )
    return CoordinationListResponse(total=len(items), items=items)


@router.post("/coordination/pickup", response_model=CoordinationListResponse)
async def pickup_coordination(body: CoordinationPickupRequest, qdrant: QdrantDep):
    items = await pickup_coordination_messages(
        qdrant,
        agent_id=body.agent_id,
        project=body.project,
        limit=body.limit,
    )
    return CoordinationListResponse(total=len(items), items=items)


@router.post("/coordination/messages/{message_id}/status", response_model=CoordinationMessageRecord)
async def set_coordination_status(message_id: str, body: CoordinationStatusUpdate, qdrant: QdrantDep):
    return await update_coordination_message_status(
        qdrant,
        message_id=message_id,
        status=body.status,
        acted_by=body.acted_by,
        action_source=body.action_source,
        reason=body.reason or "",
    )


@coordination_router.post("/messages", response_model=CoordinationMessageRecord)
async def send_coordination_message_alias(body: CoordinationMessageCreate, qdrant: QdrantDep, ollama: OllamaDep):
    return await create_coordination_message(qdrant, ollama, body)


@coordination_router.get("/messages", response_model=CoordinationListResponse)
async def list_coordination_alias(
    qdrant: QdrantDep,
    agent_id: Optional[str] = Query(None, min_length=1, max_length=128),
    project: Optional[str] = Query(None, min_length=1, max_length=128),
    mailbox: str = Query("inbox", pattern=COORDINATION_MAILBOX_PATTERN),
    thread_id: Optional[str] = Query(None, min_length=1, max_length=128),
    status: Optional[str] = Query(None, pattern=COORDINATION_STATUS_PATTERN),
    limit: int = Query(20, ge=1, le=100),
):
    items = await list_coordination_messages(
        qdrant,
        agent_id=agent_id,
        project=project,
        mailbox=mailbox,
        thread_id=thread_id,
        status=status,
        limit=limit,
    )
    return CoordinationListResponse(total=len(items), items=items)


@coordination_router.post("/pickup", response_model=CoordinationListResponse)
async def pickup_coordination_alias(body: CoordinationPickupRequest, qdrant: QdrantDep):
    items = await pickup_coordination_messages(
        qdrant,
        agent_id=body.agent_id,
        project=body.project,
        limit=body.limit,
    )
    return CoordinationListResponse(total=len(items), items=items)


@coordination_router.post("/messages/{message_id}/status", response_model=CoordinationMessageRecord)
async def set_coordination_status_alias(message_id: str, body: CoordinationStatusUpdate, qdrant: QdrantDep):
    return await update_coordination_message_status(
        qdrant,
        message_id=message_id,
        status=body.status,
        acted_by=body.acted_by,
        action_source=body.action_source,
        reason=body.reason or "",
    )


@router.get("/{model_id}")
async def get_model(model_id: str):
    """Get quota state for a single model."""
    reg = get_model_registry()
    try:
        quota = reg.get_model(model_id)
    except KeyError:
        raise HTTPException(404, f"Model '{model_id}' not registered")
    return {
        "model_id": quota.model_id,
        "display_name": quota.display_name,
        "provider": quota.provider,
        "daily_limit": quota.daily_limit,
        "limit_unit": quota.limit_unit,
        "used_today": quota.used_today,
        "remaining": quota.remaining,
        "remaining_pct": round(quota.remaining_fraction * 100, 1),
        "priority": quota.priority,
        "task_capabilities": quota.task_capabilities,
        "is_available": quota.is_available,
        "cooldown_until": quota.cooldown_until,
    }


@router.delete("/{model_id}/reset")
async def reset_quota(model_id: str):
    """Reset today's quota to 0 and clear cooldown (admin/testing)."""
    reg = get_model_registry()
    if model_id not in reg._models:
        raise HTTPException(404, f"Model '{model_id}' not registered")
    quota = reg.reset_quota(model_id)
    return {"model_id": quota.model_id, "used_today": quota.used_today, "is_available": quota.is_available}
