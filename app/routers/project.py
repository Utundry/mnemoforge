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
  POST /project/enrich-task     — attach relevant component context to a task
"""
import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import JobQueueDep, OllamaDep, QdrantDep
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


# ── Pydantic models ────────────────────────────────────────────────────────────

class ComponentSpec(BaseModel):
    """Explicit component definition for ingest."""
    component_id: str = Field(..., description="Unique ID within project, e.g. 'layout-fixer'")
    name: str = Field(..., description="Human-readable name")
    files: list[str] = Field(..., description="Absolute or relative paths to key source files")
    endpoints: list[str] = Field(default_factory=list, description="REST endpoints if applicable")


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


class RefreshRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    root_dir: str = Field("", description="Root dir to recompute hashes from (optional)")


class SearchRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(5, ge=1, le=20)


class EnrichTaskRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    task: str = Field(..., min_length=1, max_length=2000,
                      description="Task description to enrich with project context")
    max_components: int = Field(3, ge=1, le=10)


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
        )
        ingested.append(spec.component_id)

    result = {
        "project_id": body.project_id,
        "ingested": ingested,
        "skipped_unchanged": skipped,
        "total_components": len(specs),
    }
    # Trigger docs rebuild only if something actually changed
    if ingested:
        from app.services.docs_service import invalidate_docs_cache
        from app.services.job_queue import get_job_queue
        invalidate_docs_cache(body.project_id)
        await get_job_queue().submit("docs_rebuild", {"project": body.project_id})
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

    for comp in stored:
        cid = comp.get("component_id", "")
        key_files = comp.get("key_files", [])
        stored_hash = comp.get("file_hash", "")

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
        )
        updated.append(cid)

    # Trigger docs rebuild only if something actually changed
    if updated:
        from app.services.docs_service import invalidate_docs_cache
        from app.services.job_queue import get_job_queue
        invalidate_docs_cache(body.project_id)
        await get_job_queue().submit("docs_rebuild", {"project": body.project_id})
    return {"project_id": body.project_id, "updated": updated, "up_to_date": up_to_date}


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
    Enrich a task description with relevant project component context.

    Agents call this at task start to get:
    - Which components are relevant to the task
    - Purpose and implementation notes
    - Key files to look at

    This replaces the grep → read → understand loop with a single call.
    """
    svc = ProjectKnowledgeService(qdrant._client, ollama)
    await svc.ensure_collection()
    relevant = await svc.search(body.project_id, body.task, body.max_components)

    if not relevant:
        return {
            "project_id": body.project_id,
            "task": body.task,
            "context": "",
            "components": [],
            "message": "No relevant components found. Run POST /project/ingest first.",
        }

    # Build a readable context block for injection into agent prompt
    context_parts = [f"## Project Context for: {body.task}\n"]
    for r in relevant:
        context_parts.append(
            f"### {r.get('name')} ({r.get('component_id')})\n"
            f"**Purpose:** {r.get('purpose')}\n"
            f"**Implementation:** {r.get('implementation')}\n"
            f"**Key files:** {', '.join(r.get('key_files', []))}\n"
            f"**Endpoints:** {', '.join(r.get('endpoints', []))}\n"
        )
        if r.get("version_note"):
            context_parts.append(f"**Note:** {r.get('version_note')}\n")
        context_parts.append("")

    return {
        "project_id": body.project_id,
        "task": body.task,
        "context": "\n".join(context_parts),
        "components": [
            {
                "component_id": r.get("component_id"),
                "name": r.get("name"),
                "score": r.get("_score"),
                "key_files": r.get("key_files", []),
            }
            for r in relevant
        ],
    }
