"""
Project Knowledge Cache — REST API.

RepRap pattern: a project documents itself so agents can understand
components instantly, and knowledge transfers to future projects.

Endpoints:
  POST /project/ingest          — index components (explicit list or auto-scan)
  POST /project/refresh         — re-index changed components (hash-based)
  GET  /project/components      — list all components for a project
  GET  /project/component/{id}  — get one component
  POST /project/search          — semantic search across project
  POST /project/enrich-task     — attach unified project knowledge context to a task
"""
import logging
import os
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
from app.services.project_context_service import (
    assemble_project_context,
    assess_project_readiness,
    build_enrich_available_layers,
    build_handoff_compact_enrich_context,
    build_task_triage,
    build_project_bootstrap_checklist,
)
from app.services.replay_completeness_service import build_token_budget
from app.services.project_bootstrap_service import (
    bootstrap_components_from_project_memories,
    infer_project_root_hint_from_memories,
)
from app.services.data_hygiene_service import get_data_hygiene_store
from app.services.data_integrity_service import get_data_integrity_store
from app.services.learning_store import get_learning_store, make_context_signature
from app.services.project_knowledge import ProjectKnowledgeService

# Local generative model — same as used in auto_memory
MANAGER_MODEL = "qwen3:1.7b"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/project", tags=["project"])

# ── LLM prompt templates ───────────────────────────────────────────────────────

_SUMMARY_PROMPT = """\
You are a technical writer documenting a software component for an AI agent memory system.
Analyze the source code below and write a concise summary.

Component name: {name}
Files: {files}

Source:
---
{source}
---

Respond ONLY in this exact format (no extra text):
PURPOSE: <1-2 sentences: what problem this component solves>
IMPLEMENTATION: <2-3 sentences: how it is built, key patterns, main classes/functions>
STATUS: <working|wip|deprecated>
VERSION_NOTE: <optional: how this differs from the original design, or leave blank>
"""

_FILE_PATTERNS = [
    "*.py", "*.ts", "*.tsx", "*.js", "*.jsx",
    "*.go", "*.rs", "*.java", "*.kt", "*.rb",
    "*.cs", "*.cpp", "*.c", "*.h",
]
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build"}
_MAX_FILE_CHARS = 3000   # per file, to keep LLM prompt manageable
_MAX_SOURCE_CHARS = 8000  # total source per component
_DECISION_DEFAULT_WEIGHTS = {
    "impact": 0.35,
    "confidence": 0.2,
    "urgency": 0.25,
    "effort": 0.1,
    "risk": 0.1,
}


# ── Pydantic models ────────────────────────────────────────────────────────────

class ComponentSpec(BaseModel):
    """Explicit component definition for ingest."""
    component_id: str = Field(..., description="Unique ID within project, e.g. 'layout-fixer'")
    name: str = Field(..., description="Human-readable name")
    files: list[str] = Field(..., description="Absolute or relative paths to key source files")
    endpoints: list[str] = Field(default_factory=list, description="REST endpoints if applicable")


class ProjectSnapshotSpec(BaseModel):
    source_mode: str = Field(
        "workspace",
        pattern="^(workspace|git_snapshot|github_pr|archive_bundle)$",
        description="How this project snapshot was obtained.",
    )
    repo: str = Field("", max_length=512, description="Repository URL or stable repository identifier")
    branch: str = Field("", max_length=256, description="Branch name if available")
    commit_sha: str = Field("", max_length=128, description="Exact commit SHA if available")
    base_commit_sha: str = Field("", max_length=128, description="Previous documented or compared commit SHA if available")
    dirty_workspace: bool = Field(False, description="Whether the client workspace contains uncommitted local changes")
    snapshot_ts: str = Field("", max_length=128, description="Client-side snapshot timestamp if available")
    diff_summary: str = Field("", max_length=4000, description="Short diff/change summary for this snapshot")
    pr_ref: str = Field("", max_length=256, description="GitHub PR reference or similar review identifier")


class RemoteChangedFileSpec(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000, description="Repo-relative file path")
    status: str = Field(..., pattern="^(added|modified|deleted|renamed)$", description="Change status for this file")
    content: str = Field("", max_length=200000, description="Optional file content sent only when needed")
    content_hash: str = Field("", max_length=128, description="Optional hash of the file content")
    language: str = Field("", max_length=64, description="Optional language hint")
    component_hint: str = Field("", max_length=256, description="Optional component id hint")


class RemoteRenamedFileSpec(BaseModel):
    from_path: str = Field(..., min_length=1, max_length=2000)
    to_path: str = Field(..., min_length=1, max_length=2000)


class RemoteSnapshotPlanRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    storage_mode: str = Field(
        "knowledge_only",
        pattern="^(knowledge_only|selective_source_cache|full_mirror)$",
        description="How much raw source the server may retain for this remote project.",
    )
    snapshot: ProjectSnapshotSpec = Field(..., description="Explicit remote snapshot metadata from the client helper.")
    changed_files: list[str] = Field(default_factory=list, max_length=500, description="Repo-relative changed file paths.")
    deleted_files: list[str] = Field(default_factory=list, max_length=500, description="Repo-relative deleted file paths.")
    renamed_files: list[RemoteRenamedFileSpec] = Field(default_factory=list, max_length=200, description="Explicit rename records.")
    files: list[RemoteChangedFileSpec] = Field(default_factory=list, max_length=500, description="Optional selective source payload for changed files.")
    force: bool = Field(False, description="Force rebuild even if the snapshot appears unchanged.")


class RemoteSnapshotSyncRequest(RemoteSnapshotPlanRequest):
    pass


class IngestRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128,
                            description="Project identifier, e.g. 'supermemory' or 'my-app'")
    project_name: str = Field("", description="Display name for the project")
    components: list[ComponentSpec] = Field(
        default_factory=list,
        description="Explicit component list. If empty, root_dir is auto-scanned."
    )
    root_dir: str = Field("", description="Root directory for auto-scan (used when components is empty)")
    force: bool = Field(False, description="Re-index even if file hash hasn't changed")
    snapshot: ProjectSnapshotSpec | None = Field(
        None,
        description="Optional explicit code snapshot metadata for traceable project knowledge.",
    )


class RefreshRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    root_dir: str = Field("", description="Root dir to recompute hashes from (optional)")
    snapshot: ProjectSnapshotSpec | None = Field(
        None,
        description="Optional explicit code snapshot metadata for traceable refresh.",
    )
    changed_files: list[str] = Field(default_factory=list, max_length=500, description="Optional remote changed file paths.")
    files: list[RemoteChangedFileSpec] = Field(default_factory=list, max_length=500, description="Optional selective remote file payload.")


class SearchRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(5, ge=1, le=20)


class EnrichTaskRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    task: str = Field(..., min_length=1, max_length=2000,
                      description="Task description to enrich with project context")
    max_components: int = Field(3, ge=1, le=10)
    context_profile: str = Field("default", pattern="^(default|handoff_compact|hot_path)$")
    detail: str = Field(
        "compact",
        pattern="^(compact|full)$",
        description="Layer detail. handoff_compact returns a compact summary by default; full returns complete context text.",
    )
    model_context_window: int = Field(
        32000,
        ge=1000,
        description="Target main model context window used to compute context token budget.",
    )
    resume_budget_ratio: float | None = Field(
        None,
        ge=0.001,
        le=0.5,
        description="Optional override for context budget as a ratio of model_context_window.",
    )
    resume_budget_profile: str = Field(
        "handoff",
        pattern="^(normal|complex|handoff|emergency)$",
        description="Budget profile used when resume_budget_ratio is omitted.",
    )
    include_promoted_canonicals: bool = Field(
        False,
        description="Include promoted canonicals explicitly even when local knowledge is not weak yet.",
    )


class DecisionOptionCandidate(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    impact_score: float = Field(0.5, ge=0.0, le=1.0)
    confidence_score: float = Field(0.5, ge=0.0, le=1.0)
    urgency_score: float = Field(0.5, ge=0.0, le=1.0)
    effort_score: float = Field(0.5, ge=0.0, le=1.0)
    risk_score: float = Field(0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class RankDecisionOptionsRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    task: str = Field(..., min_length=1, max_length=2000)
    options: list[DecisionOptionCandidate] = Field(..., min_length=1, max_length=12)
    weights: dict[str, float] = Field(default_factory=dict, description="Optional weight overrides for impact/confidence/urgency/effort/risk")
    agent_id: str = Field("codex", min_length=1, max_length=256)
    rationale: str = Field("", max_length=2000)


class DeferredFindingCreateRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    task_id: str = Field("", max_length=256)
    finding: str = Field(..., min_length=1, max_length=2000)
    suggested_follow_up: str = Field("", max_length=2000)
    why_it_matters: str = Field("", max_length=2000)
    severity: str = Field("medium", pattern="^(low|medium|high|critical)$")
    agent_id: str = Field("codex", min_length=1, max_length=256)
    tags: list[str] = Field(default_factory=list)


class DeferredFindingPromoteRequest(BaseModel):
    acted_by: str = Field("user", min_length=1, max_length=256)
    reason: str = Field("promote_deferred_finding", max_length=500)
    importance_score: float = Field(0.7, ge=0.0, le=1.0)


class AuditFindingCreateRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    details: str = Field("", max_length=4000)
    finding_source: str = Field("manual", max_length=128)
    severity: str = Field("medium", pattern="^(low|medium|high|critical)$")
    finding_type: str = Field("manual", max_length=128)
    agent_id: str = Field("codex", min_length=1, max_length=256)
    tags: list[str] = Field(default_factory=list)


class AutoCaptureAuditFindingsRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    source: str = Field("integrity", pattern="^(integrity|hygiene)$")
    limit: int = Field(20, ge=1, le=200)
    agent_id: str = Field("codex", min_length=1, max_length=256)


class ProjectReadinessRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    auto_bootstrap_from_memories: bool = Field(
        True,
        description="When readiness finds no components, try bootstrapping components from project-scoped client-scan memories.",
    )


class BootstrapFromMemoriesRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    root_hint: str = Field(..., min_length=1, max_length=2000, description="Project root path used by remote client-scan")


class TaskTriageRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    limit: int = Field(5, ge=1, le=20)


# ── File utilities ─────────────────────────────────────────────────────────────

def _allowed_roots() -> list[Path]:
    """Return list of allowed root directories from config. Empty = no restriction."""
    from app.core.path_security import allowed_roots
    return allowed_roots()


def _check_path_allowed(p: Path) -> None:
    """Raise ValueError if path is outside allowed roots (when restriction is active)."""
    from app.core.path_security import check_path_allowed
    check_path_allowed(p)


def _read_file_safe(path: str, root: str = "") -> str:
    """Read file content, truncated to MAX_FILE_CHARS. Returns empty string on error."""
    try:
        p = Path(path) if os.path.isabs(path) else Path(root) / path
        _check_path_allowed(p)
        return p.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_CHARS]
    except ValueError as e:
        logger.warning("Ingest path blocked: %s", e)
        return ""
    except Exception:
        return ""


def _scan_dir(root_dir: str) -> dict[str, list[str]]:
    """
    Auto-discover components by scanning root_dir.
    Groups files by their immediate subdirectory.
    Returns {component_id: [file_paths]}.
    """
    root = Path(root_dir)
    if not root.exists():
        return {}
    try:
        _check_path_allowed(root)
    except ValueError as e:
        logger.warning("Scan dir blocked: %s", e)
        return {}

    groups: dict[str, list[str]] = {}
    for pattern in _FILE_PATTERNS:
        for p in root.rglob(pattern):
            # Skip unwanted dirs
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            # Group by first meaningful subdirectory relative to root
            rel = p.relative_to(root)
            parts = rel.parts
            group = parts[0] if len(parts) > 1 else "root"
            groups.setdefault(group, []).append(str(p))

    return groups


def _dedupe_str_list(values: list[str], *, limit: int) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in items:
            continue
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _normalized_remote_file_payload(files: list[RemoteChangedFileSpec]) -> list[dict]:
    rows: list[dict] = []
    seen_paths: set[str] = set()
    for file in files:
        path = str(file.path or "").strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        rows.append(
            {
                "path": path,
                "status": str(file.status or "").strip(),
                "content": file.content,
                "content_hash": str(file.content_hash or "").strip(),
                "language": str(file.language or "").strip(),
                "component_hint": str(file.component_hint or "").strip(),
            }
        )
    return rows


def _remote_payload_rows_for_component(
    *,
    component_id: str,
    key_files: list[str],
    rows: list[dict],
) -> list[dict]:
    key_file_set = {str(path).strip() for path in key_files if str(path).strip()}
    matched: list[dict] = []
    for row in rows:
        path = str(row.get("path") or "").strip()
        component_hint = str(row.get("component_hint") or "").strip()
        if component_hint and component_hint == component_id:
            matched.append(row)
            continue
        if path and path in key_file_set:
            matched.append(row)
    return matched


def _build_source_from_remote_payload(rows: list[dict]) -> str:
    parts: list[str] = []
    total = 0
    for row in rows:
        path = str(row.get("path") or "").strip()
        content = str(row.get("content") or "")
        if not path or not content:
            continue
        label = f"\n### {os.path.basename(path)}\n"
        chunk = label + content[:_MAX_FILE_CHARS]
        if total + len(chunk) > _MAX_SOURCE_CHARS:
            remaining = _MAX_SOURCE_CHARS - total
            if remaining > 100:
                parts.append(chunk[:remaining])
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)


def _build_remote_snapshot_plan(body: RemoteSnapshotPlanRequest) -> dict:
    snapshot = body.snapshot.model_dump()
    changed_files = _dedupe_str_list(body.changed_files, limit=500)
    deleted_files = _dedupe_str_list(body.deleted_files, limit=500)
    renamed_files = [
        {"from_path": str(item.from_path).strip(), "to_path": str(item.to_path).strip()}
        for item in body.renamed_files
        if str(item.from_path).strip() and str(item.to_path).strip()
    ][:200]
    file_payload = _normalized_remote_file_payload(body.files)
    file_payload_paths = {row["path"] for row in file_payload}
    changed_set = set(changed_files)
    deleted_set = set(deleted_files)
    renamed_targets = {row["to_path"] for row in renamed_files}
    source_mode = str(snapshot.get("source_mode") or "workspace").strip() or "workspace"
    commit_sha = str(snapshot.get("commit_sha") or "").strip()
    base_commit_sha = str(snapshot.get("base_commit_sha") or "").strip()
    dirty_workspace = bool(snapshot.get("dirty_workspace"))
    requires_selective_source_payload = any(
        path not in file_payload_paths for path in changed_set if path not in deleted_set
    )
    if body.storage_mode == "full_mirror":
        projection_target_state = "effective" if commit_sha else "candidate"
    elif dirty_workspace or source_mode == "workspace":
        projection_target_state = "candidate"
    else:
        projection_target_state = "effective"
    if body.force:
        rebuild_mode = "forced"
    elif commit_sha and base_commit_sha and commit_sha != base_commit_sha:
        rebuild_mode = "diff_only"
    elif commit_sha and base_commit_sha and commit_sha == base_commit_sha and not dirty_workspace:
        rebuild_mode = "skip_if_unchanged"
    elif changed_set or deleted_set or renamed_files or dirty_workspace:
        rebuild_mode = "diff_only"
    else:
        rebuild_mode = "full"
    return {
        "project_id": body.project_id,
        "storage_mode": body.storage_mode,
        "snapshot": snapshot,
        "counts": {
            "changed_files": len(changed_files),
            "deleted_files": len(deleted_files),
            "renamed_files": len(renamed_files),
            "files_with_content": len(file_payload),
        },
        "normalized": {
            "changed_files": changed_files,
            "deleted_files": deleted_files,
            "renamed_files": renamed_files,
            "file_payload_paths": sorted(file_payload_paths),
        },
        "plan": {
            "rebuild_mode": rebuild_mode,
            "projection_target_state": projection_target_state,
            "requires_selective_source_payload": requires_selective_source_payload,
            "can_skip_when_unchanged": bool(commit_sha and base_commit_sha and commit_sha == base_commit_sha and not dirty_workspace),
            "touched_paths": sorted(changed_set | deleted_set | renamed_targets),
        },
        "contract": {
            "server_default_role": "knowledge_projection",
            "client_default_role": "snapshot_and_diff_provider",
            "stores_full_repo_by_default": False,
            "stores_selective_source_cache": body.storage_mode in {"selective_source_cache", "full_mirror"},
            "full_mirror_enabled": body.storage_mode == "full_mirror",
        },
    }


def _build_source(files: list[str], root: str = "") -> str:
    """Concatenate file contents, labelled by filename, up to MAX_SOURCE_CHARS."""
    parts = []
    total = 0
    for f in files:
        content = _read_file_safe(f, root)
        if not content:
            continue
        label = f"\n### {os.path.basename(f)}\n"
        chunk = label + content
        if total + len(chunk) > _MAX_SOURCE_CHARS:
            remaining = _MAX_SOURCE_CHARS - total
            if remaining > 100:
                parts.append(chunk[:remaining])
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)


def _parse_llm_summary(text: str) -> dict:
    """Parse PURPOSE / IMPLEMENTATION / STATUS / VERSION_NOTE from LLM output."""
    result = {"purpose": "", "implementation": "", "status": "working", "version_note": ""}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("PURPOSE:"):
            result["purpose"] = line[len("PURPOSE:"):].strip()
        elif line.startswith("IMPLEMENTATION:"):
            result["implementation"] = line[len("IMPLEMENTATION:"):].strip()
        elif line.startswith("STATUS:"):
            val = line[len("STATUS:"):].strip().lower()
            if val in ("working", "wip", "deprecated"):
                result["status"] = val
        elif line.startswith("VERSION_NOTE:"):
            result["version_note"] = line[len("VERSION_NOTE:"):].strip()
    return result


async def _summarize_component_source(ollama, name: str, file_labels: list[str], source: str) -> dict:
    if not source:
        return {
            "purpose": f"Component {name}",
            "implementation": "Source files could not be read.",
            "status": "working",
            "version_note": "",
        }

    prompt = _SUMMARY_PROMPT.format(
        name=name,
        files=", ".join(os.path.basename(f) for f in file_labels),
        source=source,
    )
    try:
        raw = await ollama.generate(prompt, model=MANAGER_MODEL)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return _parse_llm_summary(raw)
    except Exception as e:
        logger.warning("LLM summarization failed for %s: %s", name, e)
        return {
            "purpose": f"Component {name} — summary unavailable.",
            "implementation": "LLM generation failed.",
            "status": "working",
            "version_note": "",
        }


def _normalize_decision_weights(raw: dict[str, float]) -> dict[str, float]:
    weights = dict(_DECISION_DEFAULT_WEIGHTS)
    for key in weights.keys():
        if key not in raw:
            continue
        try:
            value = float(raw[key])
        except Exception:
            continue
        if value < 0:
            continue
        weights[key] = value
    total = sum(weights.values())
    if total <= 0:
        return dict(_DECISION_DEFAULT_WEIGHTS)
    return {key: value / total for key, value in weights.items()}


def _decision_option_score(option: DecisionOptionCandidate, weights: dict[str, float]) -> float:
    benefit = (
        weights["impact"] * float(option.impact_score)
        + weights["confidence"] * float(option.confidence_score)
        + weights["urgency"] * float(option.urgency_score)
    )
    cost = (
        weights["effort"] * float(option.effort_score)
        + weights["risk"] * float(option.risk_score)
    )
    score = 0.5 + (benefit - cost) / 2.0
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _rank_decision_options(options: list[DecisionOptionCandidate], weights: dict[str, float]) -> list[dict]:
    ranked: list[dict] = []
    for index, option in enumerate(options):
        score = _decision_option_score(option, weights)
        ranked.append(
            {
                "candidate_index": index,
                "label": option.label,
                "description": option.description,
                "score": round(score, 4),
                "impact_score": option.impact_score,
                "confidence_score": option.confidence_score,
                "urgency_score": option.urgency_score,
                "effort_score": option.effort_score,
                "risk_score": option.risk_score,
                "tags": sorted({str(tag).strip() for tag in option.tags if str(tag).strip()}),
            }
        )
    ranked.sort(key=lambda row: (float(row["score"]), -int(row["candidate_index"])), reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    return ranked


def _parse_json_object(value: str) -> dict:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _insert_audit_finding_artifact(
    *,
    project_id: str,
    title: str,
    details: str,
    finding_source: str,
    finding_type: str,
    severity: str,
    agent_id: str,
    tags: list[str] | None = None,
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "project_id": project_id,
        "title": title,
        "details": details,
        "finding_source": finding_source,
        "finding_type": finding_type,
        "severity": severity,
        "generated_at": generated_at,
    }
    artifact_id = await get_learning_store().insert_artifact(
        agent_id=agent_id,
        artifact_type="audit_finding",
        scope="project",
        status="active",
        workflow_type="audit",
        workflow_action="capture_finding",
        workflow_context=json.dumps(payload, ensure_ascii=False),
        content=title[:4000],
        confidence=0.85,
        evidence_count=1,
        context_signature=make_context_signature(
            project=project_id,
            task_type="audit",
            phase="finding_capture",
            category="audit_finding",
            transport="api",
        ),
        tags=[f"project:{project_id}", f"severity:{severity}", "audit-finding"] + list(tags or []),
        observation=details[:1000],
        why_it_matters="Audit findings captured as reusable evolutionary artifacts.",
    )
    return str(artifact_id)


async def _summarize_component(ollama, name: str, files: list[str], root: str = "") -> dict:
    """Use local LLM to generate component summary. Returns parsed dict."""
    source = _build_source(files, root)
    if not source:
        return {
            "purpose": f"Component {name}",
            "implementation": "Source files could not be read.",
            "status": "working",
            "version_note": "",
        }

    prompt = _SUMMARY_PROMPT.format(
        name=name,
        files=", ".join(os.path.basename(f) for f in files),
        source=source,
    )
    try:
        raw = await ollama.generate(prompt, model=MANAGER_MODEL)
        # Strip qwen3 <think>...</think> blocks
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return _parse_llm_summary(raw)
    except Exception as e:
        logger.warning("LLM summarization failed for %s: %s", name, e)
        return {
            "purpose": f"Component {name} — summary unavailable.",
            "implementation": "LLM generation failed.",
            "status": "working",
            "version_note": "",
        }


# ── Background job handlers (called by JobQueue worker) ────────────────────────

async def _ingest_handler(payload: dict) -> dict:
    """Handler for 'project_ingest' background jobs."""
    from app.dependencies import get_qdrant, get_ollama
    body = IngestRequest(**payload)
    return await _run_ingest(body, get_qdrant()._client, get_ollama())


async def _refresh_handler(payload: dict) -> dict:
    """Handler for 'project_refresh' background jobs."""
    from app.dependencies import get_qdrant, get_ollama
    body = RefreshRequest(**payload)
    return await _run_refresh(body, get_qdrant()._client, get_ollama())


# ── Shared ingest/refresh logic ─────────────────────────────────────────────────

async def _run_ingest(body: IngestRequest, qdrant_client, ollama) -> dict:
    svc = ProjectKnowledgeService(qdrant_client, ollama)
    await svc.ensure_collection()

    specs: list[ComponentSpec] = body.components
    root = body.root_dir

    if not specs and root:
        groups = _scan_dir(root)
        if not groups:
            raise ValueError(f"No source files found in {root!r}")
        specs = [
            ComponentSpec(
                component_id=group_name,
                name=group_name.replace("_", " ").replace("-", " ").title(),
                files=files,
            )
            for group_name, files in groups.items()
        ]

    if not specs:
        raise ValueError("Provide either 'components' list or 'root_dir'")

    ingested = []
    skipped = []
    snapshot = body.snapshot.model_dump() if body.snapshot is not None else None

    for spec in specs:
        contents = [_read_file_safe(f, root) for f in spec.files]
        file_hash = svc.compute_hash([c for c in contents if c])

        if not body.force:
            existing = await svc.get_component(body.project_id, spec.component_id)
            if existing and existing.get("file_hash") == file_hash:
                skipped.append(spec.component_id)
                continue

        summary = await _summarize_component(ollama, spec.name, spec.files, root)
        await svc.upsert_component(
            project_id=body.project_id,
            component_id=spec.component_id,
            name=spec.name,
            purpose=summary["purpose"],
            implementation=summary["implementation"],
            key_files=spec.files,
            endpoints=spec.endpoints,
            status=summary["status"],
            file_hash=file_hash,
            version_note=summary["version_note"],
            snapshot=snapshot,
        )
        ingested.append(spec.component_id)

    result = {
        "project_id": body.project_id,
        "ingested": ingested,
        "skipped_unchanged": skipped,
        "total_components": len(specs),
        "snapshot": snapshot,
    }
    # Trigger docs rebuild only if something actually changed
    if ingested:
        from app.services.job_queue import get_job_queue
        await get_job_queue().submit(
            "docs_rebuild",
            {"project": body.project_id, "changed_component_ids": ingested},
        )
    return result


async def _run_refresh(body: RefreshRequest, qdrant_client, ollama) -> dict:
    svc = ProjectKnowledgeService(qdrant_client, ollama)
    await svc.ensure_collection()

    stored = await svc.list_components(body.project_id)
    if not stored:
        return {"project_id": body.project_id, "updated": [], "up_to_date": [],
                "message": "No components indexed yet"}

    updated = []
    up_to_date = []
    requires_source_payload = []
    snapshot = body.snapshot.model_dump() if body.snapshot is not None else None
    remote_file_payload = _normalized_remote_file_payload(body.files)
    changed_files = _dedupe_str_list(body.changed_files, limit=500)

    for comp in stored:
        cid = comp.get("component_id", "")
        key_files = comp.get("key_files", [])
        stored_hash = comp.get("file_hash", "")
        stored_snapshot = dict(comp.get("snapshot") or {})
        incoming_commit_sha = str((snapshot or {}).get("commit_sha") or "").strip()
        stored_commit_sha = str(stored_snapshot.get("commit_sha") or "").strip()
        dirty_workspace = bool((snapshot or {}).get("dirty_workspace"))

        if snapshot and not body.root_dir:
            if incoming_commit_sha and stored_commit_sha == incoming_commit_sha and not dirty_workspace:
                up_to_date.append(cid)
                continue
            matched_rows = _remote_payload_rows_for_component(
                component_id=cid,
                key_files=key_files,
                rows=remote_file_payload,
            )
            source = _build_source_from_remote_payload(matched_rows)
            if not source:
                requires_source_payload.append(cid)
                continue
            content_values = [
                str(row.get("content") or "")
                for row in matched_rows
                if str(row.get("content") or "")
            ]
            current_hash = svc.compute_hash(content_values) if content_values else stored_hash
            if current_hash == stored_hash and not dirty_workspace:
                up_to_date.append(cid)
                continue
            summary = await _summarize_component_source(
                ollama,
                comp.get("name", cid),
                [str(row.get("path") or "") for row in matched_rows if str(row.get("path") or "").strip()],
                source,
            )
            await svc.upsert_component(
                project_id=body.project_id,
                component_id=cid,
                name=comp.get("name", cid),
                purpose=summary["purpose"],
                implementation=summary["implementation"],
                key_files=key_files,
                endpoints=comp.get("endpoints", []),
                status=summary["status"],
                file_hash=current_hash,
                version_note=summary["version_note"],
                snapshot=snapshot or comp.get("snapshot"),
            )
            updated.append(cid)
            continue

        contents = [_read_file_safe(f, body.root_dir) for f in key_files]
        current_hash = svc.compute_hash([c for c in contents if c])

        if current_hash == stored_hash:
            up_to_date.append(cid)
            continue

        summary = await _summarize_component(ollama, comp.get("name", cid), key_files, body.root_dir)
        await svc.upsert_component(
            project_id=body.project_id,
            component_id=cid,
            name=comp.get("name", cid),
            purpose=summary["purpose"],
            implementation=summary["implementation"],
            key_files=key_files,
            endpoints=comp.get("endpoints", []),
            status=summary["status"],
            file_hash=current_hash,
            version_note=summary["version_note"],
            snapshot=snapshot or comp.get("snapshot"),
        )
        updated.append(cid)

    # Trigger docs rebuild only if something actually changed
    if updated:
        from app.services.job_queue import get_job_queue
        await get_job_queue().submit(
            "docs_rebuild",
            {
                "project": body.project_id,
                "changed_component_ids": updated,
                "changed_files": changed_files,
            },
        )
    return {
        "project_id": body.project_id,
        "updated": updated,
        "up_to_date": up_to_date,
        "requires_source_payload": requires_source_payload,
        "used_remote_file_payload": bool(snapshot and not body.root_dir and remote_file_payload),
        "snapshot": snapshot,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/ingest")
async def ingest_project(body: IngestRequest, qdrant: QdrantDep, ollama: OllamaDep,
                         queue: JobQueueDep, background: bool = False) -> dict:
    """
    Index project components into the knowledge cache.

    Two modes:
    - Explicit: provide `components` list with file paths per component
    - Auto-scan: provide `root_dir`, system groups files by subdirectory

    Use `?background=true` to submit as a background job and return immediately.
    Poll status at GET /tasks/{job_id}.
    """
    if background:
        job_id = await queue.submit("project_ingest", body.model_dump())
        return {"job_id": job_id, "status": "queued",
                "poll": f"/api/v1/tasks/{job_id}"}
    try:
        return await _run_ingest(body, qdrant._client, ollama)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/refresh")
async def refresh_project(body: RefreshRequest, qdrant: QdrantDep, ollama: OllamaDep,
                          queue: JobQueueDep, background: bool = False) -> dict:
    """
    Re-index components whose source files have changed (hash-based).
    Use `?background=true` to submit as a background job.
    """
    if background:
        job_id = await queue.submit("project_refresh", body.model_dump())
        return {"job_id": job_id, "status": "queued",
                "poll": f"/api/v1/tasks/{job_id}"}
    return await _run_refresh(body, qdrant._client, ollama)


@router.post("/remote-snapshot/plan")
async def plan_remote_snapshot(body: RemoteSnapshotPlanRequest) -> dict:
    """
    Validate and normalize a remote helper snapshot payload before ingest/refresh.

    This endpoint does not mutate project knowledge. It returns how the server
    will interpret the snapshot, storage mode, and rebuild/projection policy.
    """
    return _build_remote_snapshot_plan(body)


@router.post("/remote-snapshot/sync")
async def sync_remote_snapshot(body: RemoteSnapshotSyncRequest, qdrant: QdrantDep, ollama: OllamaDep) -> dict:
    """
    Helper-friendly remote snapshot workflow.

    Validates the payload, computes the server-side plan, runs project refresh,
    and returns one normalized action for the helper.
    """
    plan = _build_remote_snapshot_plan(body)
    refresh = await _run_refresh(
        RefreshRequest(
            project_id=body.project_id,
            root_dir="",
            snapshot=body.snapshot,
            changed_files=body.changed_files,
            files=body.files,
        ),
        qdrant._client,
        ollama,
    )
    if refresh.get("message") == "No components indexed yet":
        action = "bootstrap_needed"
    elif refresh.get("updated"):
        action = "refreshed"
    elif refresh.get("requires_source_payload"):
        action = "needs_source_payload"
    elif plan.get("plan", {}).get("can_skip_when_unchanged"):
        action = "skipped"
    else:
        action = "no_changes"
    return {
        "project_id": body.project_id,
        "action": action,
        "plan": plan,
        "refresh": refresh,
    }


@router.get("/components")
async def list_components(
    project_id: str = Query(..., description="Project identifier"),
    qdrant: QdrantDep = None,
    ollama: OllamaDep = None,
) -> dict:
    """List all indexed components for a project."""
    svc = ProjectKnowledgeService(qdrant._client, ollama)
    await svc.ensure_collection()
    components = await svc.list_components(project_id)
    return {
        "project_id": project_id,
        "count": len(components),
        "components": [
            {
                "component_id": c.get("component_id"),
                "name": c.get("name"),
                "status": c.get("status"),
                "purpose": c.get("purpose"),
                "endpoints": c.get("endpoints", []),
                "key_files": c.get("key_files", []),
                "file_hash": c.get("file_hash", ""),
                "version_note": c.get("version_note", ""),
                "snapshot": c.get("snapshot") or None,
            }
            for c in components
        ],
    }


@router.get("/component/{component_id}")
async def get_component(
    component_id: str,
    project_id: str = Query(...),
    qdrant: QdrantDep = None,
    ollama: OllamaDep = None,
) -> dict:
    """Get full documentation for a single component."""
    svc = ProjectKnowledgeService(qdrant._client, ollama)
    await svc.ensure_collection()
    comp = await svc.get_component(project_id, component_id)
    if not comp:
        raise HTTPException(404, f"Component '{component_id}' not found in project '{project_id}'")
    return comp


@router.post("/search")
async def search_project(body: SearchRequest, qdrant: QdrantDep, ollama: OllamaDep) -> dict:
    """
    Semantic search across project components.
    Returns components most relevant to the query.
    Useful for agents to find which component handles a given concern.
    """
    svc = ProjectKnowledgeService(qdrant._client, ollama)
    await svc.ensure_collection()
    results = await svc.search(body.project_id, body.query, body.limit)
    return {
        "project_id": body.project_id,
        "query": body.query,
        "results": [
            {
                "component_id": r.get("component_id"),
                "name": r.get("name"),
                "score": r.get("_score"),
                "purpose": r.get("purpose"),
                "implementation": r.get("implementation"),
                "status": r.get("status"),
                "endpoints": r.get("endpoints", []),
                "key_files": r.get("key_files", []),
                "version_note": r.get("version_note", ""),
            }
            for r in results
        ],
    }


@router.post("/enrich-task")
async def enrich_task(body: EnrichTaskRequest, qdrant: QdrantDep, ollama: OllamaDep) -> dict:
    """
    Enrich a task description with unified project knowledge context.

    Agents call this at task start to get:
    - Applicable laws
    - Relevant components
    - Open improvements
    - Active runtime hints
    - Recent decision memoirs
    - Effective documentation sections

    Use `context_profile='hot_path'` to return fast, minimal startup context and
    defer heavy synthesis sources for follow-up/background processing.

    This replaces the grep → read → understand loop with a memory-first retrieval call.
    """
    bundle = await assemble_project_context(
        project_id=body.project_id,
        task=body.task,
        qdrant=qdrant,
        ollama=ollama,
        context_profile=body.context_profile,
        include_promoted_canonicals=body.include_promoted_canonicals,
        max_components=body.max_components,
    )
    effective_detail = "compact" if body.context_profile == "handoff_compact" and body.detail == "compact" else "full"
    context = (
        build_handoff_compact_enrich_context(bundle)
        if effective_detail == "compact"
        else bundle.context
    )
    triage_items = list((bundle.task_triage or {}).get("items") or [])
    recommended_action = str((triage_items[0] or {}).get("recommended_action") or "").strip() if triage_items else ""
    if recommended_action and f"Next action: {recommended_action}" not in context:
        context = (context + f"\n\n## Next Action\n\nNext action: {recommended_action}").strip()
    response = {
        "project_id": bundle.project_id,
        "task": bundle.task,
        "detail": effective_detail,
        "context_profile": body.context_profile,
        "context": context,
        "components": bundle.components,
        "laws": bundle.laws,
        "improvements": bundle.improvements,
        "runtime_hints": bundle.runtime_hints,
        "memoirs": bundle.memoirs,
        "tasks": bundle.tasks,
        "task_triage": bundle.task_triage,
        "task_capture_candidates": bundle.task_capture_candidates,
        "docs_sections": bundle.docs_sections,
        "promoted_canonicals": bundle.promoted_canonicals,
        "operational_instincts": bundle.operational_instincts,
        "recommended_mcp_calls": bundle.recommended_mcp_calls,
        "coverage": bundle.coverage,
        "missing_sources": bundle.missing_sources,
        "deferred_sources": bundle.deferred_sources,
        "code_inspection_recommended": bundle.code_inspection_recommended,
        "message": bundle.message,
        "available_layers": build_enrich_available_layers(bundle),
    }
    response["token_budget"] = build_token_budget(
        response_chars=len(json.dumps(response, ensure_ascii=False, default=str)),
        model_context_window=body.model_context_window,
        resume_budget_ratio=body.resume_budget_ratio,
        resume_budget_profile=body.resume_budget_profile,
        overflow_reason="Compact enrichment preserves immediate handoff context and exposes full layers on demand.",
    )
    response["token_overhead"] = response["token_budget"]
    return response


@router.post("/task-triage")
async def task_triage(body: TaskTriageRequest, qdrant: QdrantDep) -> dict:
    """
    Rank current project tasks for agent attention using cheap, deterministic signals.

    The first slice intentionally prefers:
    - incomplete task framing
    - active/planning work
    - higher pending capture counts
    - fresher updates
    """
    return await build_task_triage(body.project_id, qdrant, limit=body.limit)


@router.post("/decision-options/rank")
async def rank_decision_options(body: RankDecisionOptionsRequest) -> dict:
    """
    Rank candidate next-step options for a project task and persist the ranked plan
    as a project-scoped artifact for later review.
    """
    weights = _normalize_decision_weights(body.weights)
    ranked_options = _rank_decision_options(body.options, weights)
    generated_at = datetime.now(timezone.utc).isoformat()
    context_signature = make_context_signature(
        project=body.project_id,
        task_type="planning",
        phase="option_selection",
        category="decision_options",
        transport="api",
    )
    plan = {
        "project_id": body.project_id,
        "task": body.task,
        "generated_at": generated_at,
        "weights": weights,
        "ranked_options": ranked_options,
        "rationale": body.rationale,
    }
    summary_lines = [
        f"Task: {body.task}",
        f"Top option: {ranked_options[0]['label']}" if ranked_options else "Top option: n/a",
        "Ranked options:",
    ]
    summary_lines.extend(
        f"{item['rank']}. {item['label']} (score={item['score']:.4f})"
        for item in ranked_options
    )
    artifact_id = await get_learning_store().insert_artifact(
        agent_id=body.agent_id,
        artifact_type="decision_options",
        scope="project",
        status="active",
        workflow_type="planning",
        workflow_action="rank_next_steps",
        workflow_context=json.dumps(plan, ensure_ascii=False),
        content="\n".join(summary_lines)[:4000],
        confidence=0.85,
        evidence_count=len(ranked_options),
        context_signature=context_signature,
        tags=[f"project:{body.project_id}", "decision-options"],
        observation=body.rationale[:1000],
        why_it_matters="Keeps ranked next-step options as reusable project planning context.",
    )
    return {
        "artifact_id": str(artifact_id),
        "project_id": body.project_id,
        "task": body.task,
        "generated_at": generated_at,
        "weights": weights,
        "ranked_options": ranked_options,
    }


@router.get("/decision-options")
async def list_decision_options(
    project_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(10, ge=1, le=50),
    include_ranked_options: bool = Query(False),
) -> dict:
    """List recent ranked decision-option plans for a project."""
    rows = await get_learning_store().list_artifacts(
        artifact_type="decision_options",
        scope="project",
        status="active",
        limit=max(limit * 3, 30),
    )
    items: list[dict] = []
    project_tag = f"project:{project_id}"
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if project_tag not in tags and f"project={project_id}" not in str(row.get("context_signature") or ""):
            continue
        parsed_plan: dict = {}
        raw_context = str(row.get("workflow_context") or "").strip()
        if raw_context:
            try:
                parsed_plan = json.loads(raw_context)
            except Exception:
                parsed_plan = {}
        ranked_options = list(parsed_plan.get("ranked_options") or [])
        top_option = ranked_options[0] if ranked_options else None
        item = {
            "artifact_id": str(row.get("id") or ""),
            "project_id": project_id,
            "task": str(parsed_plan.get("task") or ""),
            "generated_at": str(parsed_plan.get("generated_at") or ""),
            "top_option": top_option,
            "weights": parsed_plan.get("weights") or {},
            "option_count": len(ranked_options),
            "summary": str(row.get("content") or ""),
            "context_signature": str(row.get("context_signature") or ""),
            "confidence": float(row.get("confidence") or 0.0),
            "created_at": row.get("created_at"),
        }
        if include_ranked_options:
            item["ranked_options"] = ranked_options
        items.append(item)
        if len(items) >= limit:
            break

    return {
        "project_id": project_id,
        "found": len(items),
        "items": items,
    }


@router.post("/deferred-findings")
async def create_deferred_finding(body: DeferredFindingCreateRequest) -> dict:
    """
    Record a deferred finding as a first-class project-scoped event so
    non-blocking issues can be revisited during postprocessing.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    context_signature = make_context_signature(
        project=body.project_id,
        task_type="postprocessing",
        phase="deferred",
        category="deferred_finding",
        transport="api",
    )
    payload = {
        "project_id": body.project_id,
        "task_id": body.task_id,
        "finding": body.finding,
        "suggested_follow_up": body.suggested_follow_up,
        "why_it_matters": body.why_it_matters,
        "severity": body.severity,
        "generated_at": generated_at,
    }
    artifact_id = await get_learning_store().insert_artifact(
        agent_id=body.agent_id,
        artifact_type="deferred_finding",
        scope="project",
        status="active",
        workflow_type="postprocessing",
        workflow_action="defer_finding",
        workflow_context=json.dumps(payload, ensure_ascii=False),
        content=body.finding[:4000],
        confidence=0.8,
        evidence_count=1,
        context_signature=context_signature,
        tags=(
            [f"project:{body.project_id}", f"severity:{body.severity}", "deferred-finding"]
            + ([f"task_id:{body.task_id}"] if body.task_id else [])
            + list(body.tags or [])
        ),
        observation=body.suggested_follow_up[:1000],
        why_it_matters=(body.why_it_matters or "Deferred for explicit postprocessing review.")[:1000],
    )
    return {
        "artifact_id": str(artifact_id),
        "project_id": body.project_id,
        "task_id": body.task_id,
        "finding": body.finding,
        "severity": body.severity,
        "generated_at": generated_at,
    }


@router.get("/deferred-findings")
async def list_deferred_findings(
    project_id: str = Query(..., min_length=1, max_length=128),
    task_id: str = Query("", max_length=256),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """List active deferred findings for a project."""
    rows = await get_learning_store().list_artifacts(
        artifact_type="deferred_finding",
        scope="project",
        status="active",
        limit=max(limit * 3, 60),
    )
    items: list[dict] = []
    project_tag = f"project:{project_id}"
    task_tag = f"task_id:{task_id}" if task_id else ""
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if project_tag not in tags and f"project={project_id}" not in str(row.get("context_signature") or ""):
            continue
        if task_tag and task_tag not in tags and f"task_id={task_id}" not in str(row.get("context_signature") or ""):
            continue
        payload = _parse_json_object(str(row.get("workflow_context") or ""))
        items.append(
            {
                "artifact_id": str(row.get("id") or ""),
                "project_id": project_id,
                "task_id": str(payload.get("task_id") or ""),
                "finding": str(payload.get("finding") or row.get("content") or ""),
                "suggested_follow_up": str(payload.get("suggested_follow_up") or row.get("observation") or ""),
                "why_it_matters": str(payload.get("why_it_matters") or row.get("why_it_matters") or ""),
                "severity": str(payload.get("severity") or "medium"),
                "generated_at": str(payload.get("generated_at") or ""),
                "confidence": float(row.get("confidence") or 0.0),
                "tags": sorted(tags),
            }
        )
        if len(items) >= limit:
            break

    return {
        "project_id": project_id,
        "task_id": task_id,
        "found": len(items),
        "items": items,
    }


@router.post("/audit-findings")
async def create_audit_finding(body: AuditFindingCreateRequest) -> dict:
    """Create a canonical project-scoped audit-finding event."""
    artifact_id = await _insert_audit_finding_artifact(
        project_id=body.project_id,
        title=body.title,
        details=body.details,
        finding_source=body.finding_source,
        finding_type=body.finding_type,
        severity=body.severity,
        agent_id=body.agent_id,
        tags=body.tags,
    )
    return {
        "artifact_id": artifact_id,
        "project_id": body.project_id,
        "title": body.title,
        "finding_source": body.finding_source,
        "finding_type": body.finding_type,
        "severity": body.severity,
    }


@router.post("/audit-findings/auto-capture")
async def auto_capture_audit_findings(body: AutoCaptureAuditFindingsRequest) -> dict:
    """
    Auto-capture active integrity/hygiene findings into canonical project-scoped
    audit-finding artifacts for evolutionary postprocessing.
    """
    captured: list[dict] = []
    if body.source == "integrity":
        rows = get_data_integrity_store().list_findings(status="suspect", limit=body.limit)
        for row in rows:
            title = str(row.get("suspicion_type") or "Integrity finding").strip()
            details = str(row.get("details") or "")
            finding_id = str(row.get("finding_id") or "")
            artifact_id = await _insert_audit_finding_artifact(
                project_id=body.project_id,
                title=title,
                details=details,
                finding_source="data_integrity",
                finding_type=str(row.get("category") or "integrity"),
                severity="high" if float(row.get("confidence") or 0.0) >= 0.85 else "medium",
                agent_id=body.agent_id,
                tags=[f"source_finding:{finding_id}", f"slice:{row.get('slice_id') or ''}"],
            )
            captured.append({"artifact_id": artifact_id, "source_finding_id": finding_id, "title": title})
    else:
        rows = get_data_hygiene_store().list_findings(status="open", limit=body.limit)
        for row in rows:
            title = str(row.get("recommended_action") or "Hygiene finding").strip()
            details = str(row.get("details") or "")
            finding_id = str(row.get("finding_id") or "")
            severity = "high" if str(row.get("recommended_action") or "") == "delete" else "medium"
            artifact_id = await _insert_audit_finding_artifact(
                project_id=body.project_id,
                title=title,
                details=details,
                finding_source="data_hygiene",
                finding_type=str(row.get("dataset_class") or "hygiene"),
                severity=severity,
                agent_id=body.agent_id,
                tags=[f"source_finding:{finding_id}", f"store:{row.get('store_name') or ''}"],
            )
            captured.append({"artifact_id": artifact_id, "source_finding_id": finding_id, "title": title})

    return {
        "project_id": body.project_id,
        "source": body.source,
        "captured": len(captured),
        "items": captured,
    }


@router.get("/audit-findings")
async def list_audit_findings(
    project_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """List active canonical audit-finding events for a project."""
    rows = await get_learning_store().list_artifacts(
        artifact_type="audit_finding",
        scope="project",
        status="active",
        limit=max(limit * 3, 60),
    )
    items: list[dict] = []
    project_tag = f"project:{project_id}"
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if project_tag not in tags and f"project={project_id}" not in str(row.get("context_signature") or ""):
            continue
        payload = _parse_json_object(str(row.get("workflow_context") or ""))
        items.append(
            {
                "artifact_id": str(row.get("id") or ""),
                "project_id": project_id,
                "title": str(payload.get("title") or row.get("content") or ""),
                "details": str(payload.get("details") or row.get("observation") or ""),
                "finding_source": str(payload.get("finding_source") or "manual"),
                "finding_type": str(payload.get("finding_type") or "manual"),
                "severity": str(payload.get("severity") or "medium"),
                "generated_at": str(payload.get("generated_at") or ""),
                "confidence": float(row.get("confidence") or 0.0),
                "tags": sorted(tags),
            }
        )
        if len(items) >= limit:
            break
    return {
        "project_id": project_id,
        "found": len(items),
        "items": items,
    }


@router.post("/readiness")
async def project_readiness(body: ProjectReadinessRequest, qdrant: QdrantDep, ollama: OllamaDep, queue: JobQueueDep) -> dict:
    """
    Assess whether a project is ready for an external pilot workflow.

    This is the operator-facing bootstrap surface for a new or migrating project.
    It reports what project knowledge already exists, what is missing, and what
    should be done next before relying on memory-first retrieval.
    """
    report = await assess_project_readiness(
        project_id=body.project_id,
        qdrant=qdrant,
        ollama=ollama,
    )
    bootstrap_result: dict | None = None
    if body.auto_bootstrap_from_memories and int(report.coverage.get("components", 0)) == 0:
        root_hint = await infer_project_root_hint_from_memories(
            project_id=body.project_id,
            qdrant=qdrant,
        )
        if root_hint:
            bootstrap_result = await bootstrap_components_from_project_memories(
                project_id=body.project_id,
                root_hint=root_hint,
                qdrant=qdrant,
                ollama=ollama,
            )
            if bootstrap_result.get("created_components"):
                await queue.submit("docs_rebuild", {"project": body.project_id})
                report = await assess_project_readiness(
                    project_id=body.project_id,
                    qdrant=qdrant,
                    ollama=ollama,
                )
    return {
        "project_id": report.project_id,
        "readiness_level": report.readiness_level,
        "readiness_score": report.readiness_score,
        "external_pilot_ready": report.external_pilot_ready,
        "coverage": report.coverage,
        "blocking_gaps": report.blocking_gaps,
        "recommended_actions": report.recommended_actions,
        "strengths": report.strengths,
        "operational_instincts": report.operational_instincts,
        "snapshot": report.snapshot,
        "code_inspection_recommended": report.code_inspection_recommended,
        "summary": report.summary,
        "auto_bootstrap_from_memories": bootstrap_result,
    }


@router.get("/readiness")
async def project_readiness_get(
    qdrant: QdrantDep,
    ollama: OllamaDep,
    queue: JobQueueDep,
    project_id: str = Query(..., min_length=1, max_length=128),
    auto_bootstrap_from_memories: bool = Query(
        True,
        description="When readiness finds no components, try bootstrapping components from project-scoped client-scan memories.",
    ),
) -> dict:
    """Read-only alias for project readiness."""
    body = ProjectReadinessRequest(
        project_id=project_id,
        auto_bootstrap_from_memories=auto_bootstrap_from_memories,
    )
    return await project_readiness(body, qdrant=qdrant, ollama=ollama, queue=queue)


@router.post("/bootstrap-checklist")
async def project_bootstrap_checklist(body: ProjectReadinessRequest, qdrant: QdrantDep, ollama: OllamaDep) -> dict:
    """
    Return an operator-facing bootstrap checklist for a project.

    This turns readiness findings into an ordered flow so a new external project
    can be brought to a minimally usable memory-first state without hidden operator knowledge.
    """
    checklist = await build_project_bootstrap_checklist(
        project_id=body.project_id,
        qdrant=qdrant,
        ollama=ollama,
    )
    return {
        "project_id": checklist.project_id,
        "readiness_level": checklist.readiness_level,
        "bootstrap_ready": checklist.bootstrap_ready,
        "next_step": checklist.next_step,
        "steps": checklist.steps,
        "operational_instincts": checklist.operational_instincts,
        "summary": checklist.summary,
    }


@router.post("/bootstrap-from-memories")
async def bootstrap_from_memories(
    body: BootstrapFromMemoriesRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
    queue: JobQueueDep,
) -> dict:
    """
    Promote project-scoped raw client-scan memories into initial component knowledge.

    This is a bootstrap bridge for remote project pilots where raw memory already exists
    but first-class project components/docs have not been created yet.
    """
    result = await bootstrap_components_from_project_memories(
        project_id=body.project_id,
        root_hint=body.root_hint,
        qdrant=qdrant,
        ollama=ollama,
    )
    if result.get("created_components"):
        await queue.submit("docs_rebuild", {"project": body.project_id})
    return result
