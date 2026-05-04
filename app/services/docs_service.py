"""
Living Documentation Service.

Generates and caches project documentation from live data sources:
- Improvements (open/resolved)
- Project components (project knowledge cache)
- Skills marketplace
- Capability registry

Cache: qdrant_data/docs_cache/{cache_key(project)}.json
Invalidation: event-driven (no TTL). Cache is deleted on:
  - PATCH /improvements/{id}/resolve
  - POST /project/ingest (if anything changed)
  - POST /project/refresh (if anything changed)
"""
from __future__ import annotations

import json
import logging
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings
from app.models.docs import DocsSection, DocsStatus
from app.services.docs_cache_store import get_docs_cache_store
from app.services.doc_section_service import sync_effective_doc_sections
from app.services.governed_artifact import (
    apply_buffered_revision,
    discard_buffered_revision,
    stage_buffered_revision,
)
from app.services.component_docs_store import get_component_docs_store
from app.services.improvements_store import get_improvements_store
from app.services.learning_store import get_learning_store
from app.services.project_context_service import gather_project_knowledge_snapshot

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("qdrant_data") / "docs_cache"
_SAFE_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _cache_key(project: str) -> str:
    """
    Convert user-provided project id to a filesystem-safe cache key.

    If the id is already safe, keep it human-readable. Otherwise, fall back to a
    stable hashed key to prevent path traversal and invalid filenames.
    """
    raw = (project or "").strip()
    if _SAFE_PROJECT_ID_RE.fullmatch(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"project-{digest}"


def _cache_path(project: str) -> Path:
    return _CACHE_DIR / f"{_cache_key(project)}.json"


def invalidate_docs_cache(project: str) -> None:
    """Delete cached docs for a project (call before rebuild)."""
    get_docs_cache_store().delete(project)
    path = _cache_path(project)
    if path.exists():
        path.unlink()
        logger.info("Docs cache invalidated for project '%s'", project)


async def cleanup_orphaned_caches(qdrant_client: AsyncQdrantClient, collection: str) -> int:
    """
    Remove cache files for projects that no longer have data in Qdrant.
    Called once at server startup. Safe for stable/rarely-edited projects —
    their cache is only deleted if the project truly has no data.
    Returns number of files removed.
    """
    if not _CACHE_DIR.exists():
        removed = 0
    else:
        removed = 0

    projects = set(get_docs_cache_store().list_projects(limit=5000))
    if _CACHE_DIR.exists():
        for cache_file in _CACHE_DIR.glob("*.json"):
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                project = data.get("project", "")
                if not project:
                    cache_file.unlink()
                    removed += 1
                    continue
                projects.add(str(project))
            except Exception as e:
                logger.warning("Failed to check cache file %s: %s", cache_file.name, e)

    for project in sorted(projects):
        try:
            if await _project_has_any_docs_source(qdrant_client, collection, project):
                continue
            get_docs_cache_store().delete(project)
            cache_file = _cache_path(project)
            if cache_file.exists():
                cache_file.unlink()
            removed += 1
            logger.info("Removed orphaned docs cache for project '%s' (no active docs sources)", project)
        except Exception as e:
            logger.warning("Failed to check docs cache project %s: %s", project, e)

    if removed:
        logger.info("Docs cache GC: removed %d orphaned file(s)", removed)
    return removed


def _runtime_hint_matches_project(row: dict, project: str) -> bool:
    meta = row.get("meta") or {}
    if meta.get("project") == project or meta.get("project_id") == project:
        return True
    tags = {str(tag) for tag in row.get("tags") or []}
    if f"project:{project}" in tags:
        return True
    context_signature = str(row.get("context_signature") or "")
    return any(part.strip() == f"project={project}" for part in context_signature.split(";"))


def _memoir_matches_project(row: dict, project: str) -> bool:
    if row.get("project") == project or row.get("project_id") == project:
        return True
    meta = row.get("meta") or {}
    if isinstance(meta, dict):
        if meta.get("project") == project or meta.get("project_id") == project:
            return True
    tags = {str(tag) for tag in row.get("tags") or []}
    return f"project:{project}" in tags


async def _project_has_any_docs_source(qdrant_client: AsyncQdrantClient, collection: str, project: str) -> bool:
    try:
        improvements = await get_improvements_store().list(project=project, status=None, limit=1)
        if improvements:
            return True
    except Exception as exc:
        logger.debug("Improvements store check failed during docs cache GC: %s", exc)

    try:
        hints = await get_learning_store().list_artifacts(scope="runtime_hint", status="active", limit=50)
        if any(_runtime_hint_matches_project(row, project) for row in hints):
            return True
    except Exception as exc:
        logger.debug("Learning store check failed during docs cache GC: %s", exc)

    try:
        component_rows = await get_component_docs_store().list_by_project(project, limit=1)
        if component_rows:
            return True
    except Exception as exc:
        logger.debug("Component docs store check failed during docs cache GC: %s", exc)

    try:
        direct = await qdrant_client.count(
            collection_name=collection,
            count_filter=qmodels.Filter(should=[
                qmodels.Filter(must=[
                    qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="law")),
                ]),
                qmodels.Filter(must=[
                    qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task")),
                ]),
                qmodels.Filter(must=[
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
                    qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"project:{project}")),
                ]),
                qmodels.Filter(must=[
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
                    qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
                ]),
                qmodels.Filter(must=[
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
                    qmodels.FieldCondition(key="project_id", match=qmodels.MatchValue(value=project)),
                ]),
            ]),
            exact=False,
        )
        if direct.count > 0:
            return True
    except Exception as exc:
        logger.debug("Primary collection check failed during docs cache GC: %s", exc)

    try:
        collections = await qdrant_client.get_collections()
        names = {c.name for c in collections.collections}
        for pk_collection in ("project_docs", "project_knowledge"):
            if pk_collection not in names:
                continue
            project_docs = await qdrant_client.count(
                collection_name=pk_collection,
                count_filter=qmodels.Filter(should=[
                    qmodels.FieldCondition(key="project_id", match=qmodels.MatchValue(value=project)),
                    qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
                ]),
                exact=False,
            )
            if project_docs.count > 0:
                return True
    except Exception as exc:
        logger.debug("project component knowledge check failed during docs cache GC: %s", exc)

    return False


def load_docs_cache(project: str) -> Optional[DocsStatus]:
    """Load cached docs. Returns None if not built yet."""
    store_row = get_docs_cache_store().get(project)
    if store_row:
        try:
            status = DocsStatus.model_validate_json(store_row.get("status_json") or "{}")
            status.stale, status.stale_reason = _docs_cache_staleness(project, status)
            return status
        except Exception as e:
            logger.warning("Failed to load docs cache from SQLite for '%s': %s", project, e)
    path = _cache_path(project)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        status = DocsStatus.model_validate(data)
        get_docs_cache_store().upsert(project, status.model_dump_json())
        status.stale, status.stale_reason = _docs_cache_staleness(project, status)
        return status
    except Exception as e:
        logger.warning("Failed to load docs cache for '%s': %s", project, e)
        return None


def _save_docs_cache(project: str, status: DocsStatus) -> None:
    get_docs_cache_store().upsert(project, status.model_dump_json())
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(project).write_text(
        status.model_dump_json(indent=2), encoding="utf-8"
    )
    logger.info("Docs cache saved for project '%s'", project)


def _latest_component_snapshot(snapshot: dict) -> dict[str, str | bool]:
    components = list(snapshot.get("components") or [])
    rows = [dict(item.get("snapshot") or {}) for item in components if isinstance(item, dict) and item.get("snapshot")]
    if not rows:
        return {}
    commit_shas = [str(item.get("commit_sha") or "").strip() for item in rows if str(item.get("commit_sha") or "").strip()]
    if not commit_shas:
        return {}
    dirty_workspace = any(bool(item.get("dirty_workspace")) for item in rows)
    source_modes = [str(item.get("source_mode") or "").strip() for item in rows if str(item.get("source_mode") or "").strip()]
    repos = [str(item.get("repo") or "").strip() for item in rows if str(item.get("repo") or "").strip()]
    branches = [str(item.get("branch") or "").strip() for item in rows if str(item.get("branch") or "").strip()]
    base_commits = [str(item.get("base_commit_sha") or "").strip() for item in rows if str(item.get("base_commit_sha") or "").strip()]
    return {
        "source_mode": source_modes[-1] if source_modes else "workspace",
        "repo": repos[-1] if repos else "",
        "branch": branches[-1] if branches else "",
        "commit_sha": commit_shas[-1] if commit_shas else "",
        "base_commit_sha": base_commits[-1] if base_commits else "",
        "dirty_workspace": dirty_workspace,
    }


def _latest_component_snapshot_from_rows(rows: list[dict]) -> dict[str, str | bool]:
    return _latest_component_snapshot(
        {
            "components": [
                {"snapshot": row.get("snapshot") or {}}
                for row in rows
                if isinstance(row, dict) and row.get("snapshot")
            ]
        }
    )


def _snapshot_signature(snapshot: dict[str, str | bool] | None) -> tuple[str, str, str, str, bool]:
    snapshot = snapshot or {}
    return (
        str(snapshot.get("source_mode") or ""),
        str(snapshot.get("repo") or ""),
        str(snapshot.get("branch") or ""),
        str(snapshot.get("commit_sha") or ""),
        bool(snapshot.get("dirty_workspace")),
    )


def _display_commit(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 40 and all(ch in "0123456789abcdefABCDEF" for ch in value):
        return value[:12]
    return value


def _docs_cache_staleness(project: str, cached: DocsStatus) -> tuple[bool, str | None]:
    try:
        current_rows = get_component_docs_store().list_by_project_sync(project, limit=500)
    except Exception as exc:
        logger.debug("Failed to read current component docs for staleness check: %s", exc)
        return False, None
    current_snapshot = _latest_component_snapshot_from_rows(current_rows)
    if not current_snapshot or not cached.snapshot:
        return False, None

    current_sig = _snapshot_signature(current_snapshot)
    cached_sig = _snapshot_signature(cached.snapshot)
    if current_sig == cached_sig:
        return False, None

    current_commit = str(current_snapshot.get("commit_sha") or "").strip()
    cached_commit = str((cached.snapshot or {}).get("commit_sha") or "").strip()
    if current_commit and cached_commit and current_commit != cached_commit:
        return True, (
            "component snapshot commit changed from "
            f"{_display_commit(cached_commit)} to {_display_commit(current_commit)}"
        )
    if bool(current_snapshot.get("dirty_workspace")) != bool((cached.snapshot or {}).get("dirty_workspace")):
        return True, "component snapshot dirty-workspace state changed"
    return True, "component snapshot metadata changed"


def _resolve_changed_component_scope(
    *,
    components: list[dict],
    changed_component_ids: list[str] | None,
    changed_files: list[str] | None,
) -> set[str]:
    explicit = {str(item).strip() for item in (changed_component_ids or []) if str(item).strip()}
    if explicit:
        return explicit
    changed_paths = {str(item).strip() for item in (changed_files or []) if str(item).strip()}
    if not changed_paths:
        return set()
    impacted: set[str] = set()
    for component in components:
        component_id = str(component.get("component_id") or "").strip()
        key_files = {str(path).strip() for path in component.get("key_files") or [] if str(path).strip()}
        if component_id and key_files.intersection(changed_paths):
            impacted.add(component_id)
    return impacted


async def sync_docs_projection_memory(project: str, qdrant, ollama) -> list[str]:
    cached = load_docs_cache(project)
    if not cached:
        return []
    return await sync_effective_doc_sections(qdrant, ollama, cached)


def apply_docs_candidate(
    project: str,
    *,
    reviewed_by: str = "user",
    review_source: str = "inline_user_approval",
    reason: str = "",
) -> DocsStatus:
    cached = load_docs_cache(project)
    if not cached:
        raise ValueError(f"Docs not built for project '{project}'")
    sections, generated_at, candidate_sections, candidate_generated_at = apply_buffered_revision(
        effective_value=cached.sections,
        effective_updated_at=cached.generated_at,
        candidate_value=cached.candidate_sections,
        candidate_updated_at=cached.candidate_generated_at or datetime.now(timezone.utc),
        empty_factory=dict,
    )
    promoted = DocsStatus(
        project=cached.project,
        generated_at=generated_at,
        sections=sections,
        snapshot=dict(cached.snapshot or {}),
        stale=cached.stale,
        stale_reason=cached.stale_reason,
        last_rebuild_mode=cached.last_rebuild_mode,
        candidate_generated_at=candidate_generated_at,
        candidate_sections=candidate_sections,
        last_review_action="apply_candidate",
        last_reviewed_by=reviewed_by,
        last_review_source=review_source,
        last_reviewed_at=datetime.now(timezone.utc),
        last_review_reason=reason or None,
    )
    _save_docs_cache(project, promoted)
    return promoted


def discard_docs_candidate(
    project: str,
    *,
    reviewed_by: str = "user",
    review_source: str = "inline_user_approval",
    reason: str = "",
) -> DocsStatus:
    cached = load_docs_cache(project)
    if not cached:
        raise ValueError(f"Docs not built for project '{project}'")
    sections, generated_at, candidate_sections, candidate_generated_at = discard_buffered_revision(
        effective_value=cached.sections,
        effective_updated_at=cached.generated_at,
        candidate_value=cached.candidate_sections,
        empty_factory=dict,
    )
    updated = DocsStatus(
        project=cached.project,
        generated_at=generated_at,
        sections=sections,
        snapshot=dict(cached.snapshot or {}),
        stale=cached.stale,
        stale_reason=cached.stale_reason,
        last_rebuild_mode=cached.last_rebuild_mode,
        candidate_generated_at=candidate_generated_at,
        candidate_sections=candidate_sections,
        last_review_action="discard_candidate",
        last_reviewed_by=reviewed_by,
        last_review_source=review_source,
        last_reviewed_at=datetime.now(timezone.utc),
        last_review_reason=reason or None,
    )
    _save_docs_cache(project, updated)
    return updated


# ── Section generators ────────────────────────────────────────────────────────

async def _fetch_improvements(qdrant_client: AsyncQdrantClient, collection: str, project: str) -> list[dict]:
    results, _ = await qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="improvement")),
            qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
        ]),
        limit=500,
        with_payload=True,
        with_vectors=False,
    )
    return [r.payload for r in results if r.payload]


def _skill_matches_project_scope(skill: dict, project: str) -> bool:
    if not project:
        return False
    if skill.get("project") == project or skill.get("project_id") == project:
        return True

    meta = skill.get("meta") or {}
    if isinstance(meta, dict):
        if meta.get("project") == project or meta.get("project_id") == project:
            return True

    tags = {str(tag).strip().lower() for tag in (skill.get("tags") or []) if str(tag).strip()}
    return f"project:{project.lower()}" in tags


async def _fetch_skills(qdrant_client: AsyncQdrantClient, collection: str, project: str) -> list[dict]:
    results, _ = await qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
        ]),
        limit=500,
        with_payload=True,
        with_vectors=False,
    )
    skills = [r.payload for r in results if r.payload]
    if not skills:
        return []

    if project == settings.self_project_id:
        return skills

    scoped = [skill for skill in skills if _skill_matches_project_scope(skill, project)]
    pinned = [skill for skill in skills if bool(skill.get("pinned"))]
    if scoped:
        return scoped + [skill for skill in pinned if skill not in scoped]
    return pinned


async def _fetch_laws(qdrant_client: AsyncQdrantClient, collection: str, project: str) -> list[dict]:
    results, _ = await qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="law")),
            qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
            qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value="active")),
        ]),
        limit=200,
        with_payload=True,
        with_vectors=False,
    )
    return [r.payload for r in results if r.payload]


async def _fetch_components(qdrant_client: AsyncQdrantClient, project: str) -> list[dict]:
    pk_collection = "project_docs"
    try:
        collections = await qdrant_client.get_collections()
        names = [c.name for c in collections.collections]
        if pk_collection not in names:
            return []
        results, _ = await qdrant_client.scroll(
            collection_name=pk_collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="project_id", match=qmodels.MatchValue(value=project)),
            ]),
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        return [r.payload for r in results if r.payload]
    except Exception as e:
        logger.warning("Failed to fetch project components: %s", e)
        return []


def _gen_features(improvements: list[dict]) -> str:
    resolved = [i for i in improvements if i.get("status") == "resolved"]
    if not resolved:
        return "_No resolved improvements yet._"
    resolved.sort(key=lambda x: -(x.get("importance_score") or 0))
    lines = ["| Improvement | Importance | Tags |", "|-------------|------------|------|"]
    for i in resolved[:20]:
        tags = ", ".join(i.get("tags") or [])
        lines.append(f"| {i.get('title', '?')} | {i.get('importance_score', 0):.2f} | {tags} |")
    return "\n".join(lines)


def _gen_pending(improvements: list[dict]) -> str:
    open_ = [i for i in improvements if i.get("status") == "open"]
    if not open_:
        return "_No open improvements. Everything is resolved!_"
    open_.sort(key=lambda x: -(x.get("importance_score") or 0))
    lines = ["| Improvement | Importance | Tags |", "|-------------|------------|------|"]
    for i in open_[:20]:
        tags = ", ".join(i.get("tags") or [])
        lines.append(f"| {i.get('title', '?')} | {i.get('importance_score', 0):.2f} | {tags} |")
    return "\n".join(lines)


def _gen_runtime_hints(runtime_hints: list[dict]) -> str:
    if not runtime_hints:
        return "_No active runtime hints._"
    lines = []
    for item in runtime_hints[:20]:
        label = item.get("label") or item.get("content") or "runtime hint"
        lines.append(f"- **{label}**")
        if item.get("content"):
            lines.append(f"  - Guidance: {item['content']}")
        elif item.get("observation"):
            lines.append(f"  - Observation: {item['observation']}")
        lines.append(
            f"  - Confidence: {float(item.get('confidence') or 0.0):.2f}, "
            f"Evidence: {int(item.get('evidence_count') or 0)}"
        )
    return "\n".join(lines)


def _gen_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "_No active or recent project tasks._"
    lines = []
    for item in tasks[:20]:
        lines.append(f"- **{item['title']}** [{item['status']}]")
        if item.get("latest_change_type") and item.get("latest_change_summary"):
            lines.append(f"  - Latest change ({item['latest_change_type']}): {item['latest_change_summary']}")
    return "\n".join(lines)


def _gen_skills(skills: list[dict]) -> str:
    if not skills:
        return "_No skills published yet._"
    domain_counts: dict[str, int] = {}
    for s in skills:
        for tag in (s.get("domain_tags") or s.get("tags") or []):
            domain_counts[tag] = domain_counts.get(tag, 0) + 1
    top_domains = sorted(domain_counts.items(), key=lambda x: -x[1])[:10]
    lines = [
        f"**Total skills:** {len(skills)}",
        "",
        "**Top domains:**",
    ]
    for domain, count in top_domains:
        lines.append(f"- `{domain}` — {count}")
    lines.append("")
    lines.append("**Recently published:**")
    for s in skills[:8]:
        name = s.get("skill_name") or s.get("source", "?").replace("skill-publish:", "")
        platform = s.get("platform", "")
        lines.append(f"- **{name}** ({platform})")
    return "\n".join(lines)


def _gen_laws(laws: list[dict]) -> str:
    if not laws:
        return "_No active project laws._"
    lines = []
    for law in laws:
        meta = law.get("meta") or {}
        content = str(law.get("content") or "")
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        title = meta.get("title") or first_line.replace("Law: ", "", 1) or "Untitled law"
        statement = meta.get("statement") or content.strip() or "_No statement recorded._"
        rationale = meta.get("rationale") or ""
        lines.append(f"- **{title}**: {statement}")
        if rationale:
            lines.append(f"  - Why: {rationale}")
    return "\n".join(lines)


def _gen_performance(project: str) -> str:
    try:
        from app.services.capability_registry import CapabilityRegistry
        reg = CapabilityRegistry(Path("qdrant_data") / "capabilities.json")
        task_types = ["code_generation", "memory_extraction", "fact_extraction",
                      "text_summarization", "skill_tagging", "layout_fix", "log_filter"]
        lines = ["| Component | Task | Score |", "|-----------|------|-------|"]
        for task in task_types:
            best = reg.best_for(task)
            for component, score in best[:2]:
                lines.append(f"| `{component}` | {task} | {score:.2f} |")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to generate performance section: %s", e)
        return "_Performance data unavailable._"


def _fallback_overview(
    improvements: list[dict],
    components: list[dict],
    runtime_hints: list[dict],
    tasks: list[dict],
    project: str,
) -> str:
    total_imp = len(improvements)
    resolved = sum(1 for i in improvements if i.get("status") == "resolved")
    open_ = total_imp - resolved
    return (
        f"**{project}** - {total_imp} improvements tracked "
        f"({resolved} resolved, {open_} open). "
        f"{len(components)} components indexed. "
        f"{len(runtime_hints)} active runtime hints. "
        f"{len(tasks)} recent tasks."
    )


def _fallback_architecture(components: list[dict]) -> str:
    if not components:
        return "_No components indexed yet. Run `POST /project/ingest` first._"
    comp_lines = [
        f"- **{c.get('name')}** (`{c.get('component_id')}`): {c.get('purpose', '')}"
        for c in components
    ]
    return "**Components:**\n\n" + "\n".join(comp_lines)


def _looks_like_generation_leak(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    lower = body.lower()
    markers = (
        "i need to create",
        "i need to write",
        "let me analyze",
        "let me organize",
        "first, let me",
        "here's my",
        "here is my",
        "the summary should",
        "be concise and factual",
        "i would need",
        "i will create",
        "i will write",
        "я создам",
        "я напишу",
        "позвольте",
        "давайте",
    )
    if "```" in body:
        return True
    if any(marker in lower for marker in markers):
        return True
    return lower.startswith(("i need to ", "let me ", "first, ", "here is ", "here's ", "я ", "сначала "))


async def _gen_overview(
    improvements: list[dict],
    components: list[dict],
    runtime_hints: list[dict],
    tasks: list[dict],
    project: str,
    *,
    use_cloud: bool = True,
) -> str:
    from app.services.cloud_llm import cloud_available
    from app.services.llm_gateway import get_cloud_gateway

    fallback = _fallback_overview(improvements, components, runtime_hints, tasks, project)

    total_imp = len(improvements)
    resolved = sum(1 for i in improvements if i.get("status") == "resolved")
    open_ = total_imp - resolved

    if not use_cloud or not cloud_available():
        return fallback

    comp_names = ", ".join(c.get("name", "") for c in components[:8]) or "none"
    top_open = sorted([i for i in improvements if i.get("status") == "open"],
                      key=lambda x: -(x.get("importance_score") or 0))[:5]
    open_list = "\n".join(f"- {i.get('title')}" for i in top_open) or "none"

    prompt = f"""Write a 2-3 sentence executive summary for the {project} project documentation.

Stats: {total_imp} improvements total ({resolved} resolved / {open_ } open), {len(components)} components, {len(runtime_hints)} active runtime hints, {len(tasks)} recent tasks.
Key components: {comp_names}
Top pending: {open_list}

Be concise and factual. Markdown format."""
    try:
        generated = await get_cloud_gateway().generate(
            prompt,
            system="You are a concise technical writer. Prefer compact factual summaries.",
            task_type="text_summarization",
            mode="economy",
            max_tokens=220,
            temperature=0.2,
            allow_local_fallback=False,
        )
        if _looks_like_generation_leak(generated):
            logger.warning("GLM overview returned prompt leakage; using deterministic fallback")
            return fallback
        return generated
    except Exception as e:
        logger.warning("GLM overview failed: %s", e)
        return fallback


async def _gen_architecture(components: list[dict], project: str, *, use_cloud: bool = True) -> str:
    from app.services.cloud_llm import cloud_available
    from app.services.llm_gateway import get_cloud_gateway

    fallback = _fallback_architecture(components)

    if not components:
        return fallback

    comp_lines = []
    for c in components:
        comp_lines.append(
            f"- **{c.get('name')}** (`{c.get('component_id')}`): {c.get('purpose', '')}"
        )

    if not use_cloud or not cloud_available():
        return fallback

    prompt = f"""Describe the architecture of the {project} project based on its components.
Write 3-4 sentences covering: overall structure, key interactions, main data flows.

Components:
{chr(10).join(comp_lines)}

Be concise. Markdown format."""
    try:
        synthesis = await get_cloud_gateway().generate(
            prompt,
            system="You describe software architecture precisely and compactly in Markdown.",
            task_type="architecture",
            mode="economy",
            max_tokens=320,
            temperature=0.2,
            allow_local_fallback=False,
        )
        if _looks_like_generation_leak(synthesis):
            logger.warning("GLM architecture returned prompt leakage; using deterministic fallback")
            return fallback
        return synthesis + "\n\n**Components:**\n\n" + "\n".join(comp_lines)
    except Exception as e:
        logger.warning("GLM architecture failed: %s", e)
        return fallback


async def _fetch_memoirs(qdrant_client: AsyncQdrantClient, collection: str, project: str, limit: int = 10) -> list[dict]:
    """Fetch recent task memoirs (category=task_memoir)."""
    from app.services.memoir_service import hydrate_memoir_payload_entries

    try:
        results, _ = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
                ],
                should=[
                    qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"project:{project}")),
                    qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project)),
                    qmodels.FieldCondition(key="project_id", match=qmodels.MatchValue(value=project)),
                ],
            ),
            limit=max(limit * 5, 50),
            with_payload=True,
            with_vectors=False,
        )
        entries = [{"id": str(getattr(r, "id", "") or ""), "payload": dict(r.payload or {})} for r in results if r.payload]
        entries = await hydrate_memoir_payload_entries(entries)
        payloads = [
            entry.get("payload") or {}
            for entry in entries
            if entry.get("payload") and _memoir_matches_project(entry.get("payload") or {}, project)
        ]
        payloads = [
            payload for payload in payloads
            if ((payload.get("meta") or {}).get("quality_status") or "").lower() != "weak"
        ]
        payloads.sort(key=lambda p: p.get("timestamp", ""), reverse=True)
        return payloads[:limit]
    except Exception as e:
        logger.warning("Failed to fetch memoirs: %s", e)
        return []


def _gen_decisions(memoirs: list[dict]) -> str:
    """Deterministic aggregation of task memoirs into the decisions section."""
    if not memoirs:
        return "_No task memoirs yet. Memoirs are generated automatically when improvements are resolved._"

    parts = []
    for m in memoirs:
        content = m.get("content", "")
        ts = m.get("timestamp", "")
        date = ts[:10] if ts else "?"
        # Strip leading "# Memoir: " heading to avoid double-heading in rendered output
        lines = content.splitlines()
        if lines and lines[0].startswith("# Memoir:"):
            title = lines[0].replace("# Memoir:", "").strip()
            body = "\n".join(lines[1:]).strip()
        else:
            title = f"Task ({date})"
            body = content.strip()
        parts.append(f"### {title} _{date}_\n\n{body}")

    return "\n\n---\n\n".join(parts)


# ── Main rebuild ──────────────────────────────────────────────────────────────

async def rebuild_docs(
    project: str,
    qdrant_client: AsyncQdrantClient,
    collection: str,
    *,
    force: bool = False,
    changed_component_ids: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> DocsStatus:
    """Gather all data and regenerate docs. Saves to cache. Returns DocsStatus."""
    logger.info("Rebuilding docs for project '%s'", project)

    snapshot = await gather_project_knowledge_snapshot(
        project_id=project,
        qdrant=qdrant_client,
        collection=collection,
        ollama=None,
    )
    improvements = snapshot["improvements"]
    laws = snapshot["laws"]
    components = snapshot["components"]
    memoirs = snapshot["memoirs"]
    runtime_hints = snapshot["runtime_hints"]
    tasks = snapshot["tasks"]
    skills = await _fetch_skills(qdrant_client, collection, project)
    snapshot_meta = _latest_component_snapshot(snapshot)
    existing = load_docs_cache(project)
    impacted_component_ids = _resolve_changed_component_scope(
        components=components,
        changed_component_ids=changed_component_ids,
        changed_files=changed_files,
    )
    narrow_component_diff = bool(existing and impacted_component_ids and len(impacted_component_ids) <= 2 and not force)

    if (
        not force
        and existing is not None
        and snapshot_meta
        and str(snapshot_meta.get("commit_sha") or "").strip()
        and str((existing.snapshot or {}).get("commit_sha") or "").strip() == str(snapshot_meta.get("commit_sha") or "").strip()
        and not bool(snapshot_meta.get("dirty_workspace"))
    ):
        logger.info(
            "Skipping docs rebuild for project '%s' because snapshot commit is unchanged (%s)",
            project,
            str(snapshot_meta.get("commit_sha") or "").strip()[:12],
        )
        return existing

    sections: dict[str, DocsSection] = {
        "overview": DocsSection(
            name="Overview",
            content=await _gen_overview(
                improvements,
                components,
                runtime_hints,
                tasks,
                project,
                use_cloud=not narrow_component_diff,
            )
        ),
        "architecture": DocsSection(
            name="Architecture",
            content=await _gen_architecture(
                components,
                project,
                use_cloud=not narrow_component_diff,
            )
        ),
        "laws": DocsSection(name="Active Project Laws", content=_gen_laws(laws)),
        "features": DocsSection(name="Resolved Features", content=_gen_features(improvements)),
        "pending": DocsSection(name="Pending Improvements", content=_gen_pending(improvements)),
        "runtime_hints": DocsSection(name="Active Runtime Hints", content=_gen_runtime_hints(runtime_hints)),
        "tasks": DocsSection(name="Recent Project Tasks", content=_gen_tasks(tasks)),
        "decisions": DocsSection(name="Decision Log", content=_gen_decisions(memoirs)),
        "skills": DocsSection(name="Cross-Project Skills Marketplace", content=_gen_skills(skills)),
        "performance": DocsSection(name="Global Model Performance", content=_gen_performance(project)),
    }

    generated_at = datetime.now(timezone.utc)
    effective_sections, effective_generated_at, candidate_sections, candidate_generated_at = stage_buffered_revision(
        effective_value=dict(existing.sections) if existing else {},
        effective_updated_at=existing.generated_at if existing else generated_at,
        replacement_value=sections,
        replacement_updated_at=generated_at,
        preserve_effective=bool(existing and existing.sections and not force),
        empty_factory=dict,
    )
    status = DocsStatus(
        project=project,
        generated_at=effective_generated_at,
        sections=effective_sections,
        snapshot=snapshot_meta,
        stale=False,
        stale_reason=None,
        last_rebuild_mode=(
            "candidate_overlay"
            if bool(snapshot_meta.get("dirty_workspace"))
            else ("diff_scoped" if narrow_component_diff else "rebuild")
        ),
        candidate_generated_at=candidate_generated_at,
        candidate_sections=candidate_sections,
        last_review_action=existing.last_review_action if existing else None,
        last_reviewed_by=existing.last_reviewed_by if existing else None,
        last_review_source=existing.last_review_source if existing else None,
        last_reviewed_at=existing.last_reviewed_at if existing else None,
        last_review_reason=existing.last_review_reason if existing else None,
    )
    _save_docs_cache(project, status)
    return status
