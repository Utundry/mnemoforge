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

from app.models.docs import DocsSection, DocsStatus

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
        return 0

    removed = 0
    for cache_file in _CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            project = data.get("project", "")
            if not project:
                cache_file.unlink()
                removed += 1
                continue

            # Check if project has any data in Qdrant (improvements, skills, or components)
            result = await qdrant_client.count(
                collection_name=collection,
                count_filter=qmodels.Filter(should=[
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="improvement")),
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
                ]),
                exact=False,
            )
            # Also check project_knowledge collection
            pk_has_data = False
            try:
                collections = await qdrant_client.get_collections()
                if "project_knowledge" in [c.name for c in collections.collections]:
                    pk_result = await qdrant_client.count(
                        collection_name="project_knowledge",
                        exact=False,
                    )
                    pk_has_data = pk_result.count > 0
            except Exception:
                pass

            if result.count == 0 and not pk_has_data:
                cache_file.unlink()
                removed += 1
                logger.info("Removed orphaned docs cache: %s (project='%s', no data in Qdrant)", cache_file.name, project)
        except Exception as e:
            logger.warning("Failed to check cache file %s: %s", cache_file.name, e)

    if removed:
        logger.info("Docs cache GC: removed %d orphaned file(s)", removed)
    return removed


def load_docs_cache(project: str) -> Optional[DocsStatus]:
    """Load cached docs. Returns None if not built yet."""
    path = _cache_path(project)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DocsStatus.model_validate(data)
    except Exception as e:
        logger.warning("Failed to load docs cache for '%s': %s", project, e)
        return None


def _save_docs_cache(project: str, status: DocsStatus) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(project).write_text(
        status.model_dump_json(indent=2), encoding="utf-8"
    )
    logger.info("Docs cache saved for project '%s'", project)


# ── Section generators ────────────────────────────────────────────────────────

async def _fetch_improvements(qdrant_client: AsyncQdrantClient, collection: str) -> list[dict]:
    results, _ = await qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="improvement")),
        ]),
        limit=500,
        with_payload=True,
        with_vectors=False,
    )
    return [r.payload for r in results if r.payload]


async def _fetch_skills(qdrant_client: AsyncQdrantClient, collection: str) -> list[dict]:
    results, _ = await qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=qmodels.Filter(must=[
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
        ]),
        limit=500,
        with_payload=True,
        with_vectors=False,
    )
    return [r.payload for r in results if r.payload]


async def _fetch_components(qdrant_client: AsyncQdrantClient) -> list[dict]:
    pk_collection = "project_knowledge"
    try:
        collections = await qdrant_client.get_collections()
        names = [c.name for c in collections.collections]
        if pk_collection not in names:
            return []
        results, _ = await qdrant_client.scroll(
            collection_name=pk_collection,
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


async def _gen_overview(improvements: list[dict], skills: list[dict],
                        components: list[dict], project: str) -> str:
    from app.services.cloud_llm import cloud_available, cloud_complete

    total_imp = len(improvements)
    resolved = sum(1 for i in improvements if i.get("status") == "resolved")
    open_ = total_imp - resolved

    if not cloud_available():
        return (
            f"**{project}** — {total_imp} improvements tracked "
            f"({resolved} resolved, {open_} open). "
            f"{len(skills)} skills published. "
            f"{len(components)} components indexed."
        )

    comp_names = ", ".join(c.get("name", "") for c in components[:8]) or "none"
    top_open = sorted([i for i in improvements if i.get("status") == "open"],
                      key=lambda x: -(x.get("importance_score") or 0))[:5]
    open_list = "\n".join(f"- {i.get('title')}" for i in top_open) or "none"

    prompt = f"""Write a 2-3 sentence executive summary for the {project} project documentation.

Stats: {total_imp} improvements total ({resolved} resolved / {open_ } open), {len(skills)} skills, {len(components)} components.
Key components: {comp_names}
Top pending: {open_list}

Be concise and factual. Markdown format."""
    try:
        return await cloud_complete(prompt, max_tokens=300, temperature=0.2)
    except Exception as e:
        logger.warning("GLM overview failed: %s", e)
        return (
            f"**{project}** — {total_imp} improvements tracked "
            f"({resolved} resolved, {open_} open). "
            f"{len(skills)} skills, {len(components)} components."
        )


async def _gen_architecture(components: list[dict], project: str) -> str:
    from app.services.cloud_llm import cloud_available, cloud_complete

    if not components:
        return "_No components indexed yet. Run `POST /project/ingest` first._"

    comp_lines = []
    for c in components:
        comp_lines.append(
            f"- **{c.get('name')}** (`{c.get('component_id')}`): {c.get('purpose', '')}"
        )

    if not cloud_available():
        return "**Components:**\n\n" + "\n".join(comp_lines)

    prompt = f"""Describe the architecture of the {project} project based on its components.
Write 3-4 sentences covering: overall structure, key interactions, main data flows.

Components:
{chr(10).join(comp_lines)}

Be concise. Markdown format."""
    try:
        synthesis = await cloud_complete(prompt, max_tokens=400, temperature=0.2)
        return synthesis + "\n\n**Components:**\n\n" + "\n".join(comp_lines)
    except Exception as e:
        logger.warning("GLM architecture failed: %s", e)
        return "**Components:**\n\n" + "\n".join(comp_lines)


async def _fetch_memoirs(qdrant_client: AsyncQdrantClient, collection: str, limit: int = 10) -> list[dict]:
    """Fetch recent task memoirs (category=task_memoir)."""
    try:
        results, _ = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="task_memoir")),
            ]),
            limit=limit * 2,  # fetch extra, sort by timestamp below
            with_payload=True,
            with_vectors=False,
        )
        payloads = [r.payload for r in results if r.payload]
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

async def rebuild_docs(project: str, qdrant_client: AsyncQdrantClient, collection: str) -> DocsStatus:
    """Gather all data and regenerate docs. Saves to cache. Returns DocsStatus."""
    logger.info("Rebuilding docs for project '%s'", project)

    improvements, skills, components, memoirs = (
        await _fetch_improvements(qdrant_client, collection),
        await _fetch_skills(qdrant_client, collection),
        await _fetch_components(qdrant_client),
        await _fetch_memoirs(qdrant_client, collection),
    )

    sections: dict[str, DocsSection] = {
        "overview": DocsSection(
            name="Overview",
            content=await _gen_overview(improvements, skills, components, project)
        ),
        "architecture": DocsSection(
            name="Architecture",
            content=await _gen_architecture(components, project)
        ),
        "features": DocsSection(name="Resolved Features", content=_gen_features(improvements)),
        "pending": DocsSection(name="Pending Improvements", content=_gen_pending(improvements)),
        "decisions": DocsSection(name="Decision Log", content=_gen_decisions(memoirs)),
        "skills": DocsSection(name="Skills Marketplace", content=_gen_skills(skills)),
        "performance": DocsSection(name="Model Performance", content=_gen_performance(project)),
    }

    status = DocsStatus(
        project=project,
        generated_at=datetime.now(timezone.utc),
        sections=sections,
    )
    _save_docs_cache(project, status)
    return status
