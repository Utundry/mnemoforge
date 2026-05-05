"""
Living Documentation API.

Endpoints:
  POST /docs/rebuild                  - trigger background docs rebuild
  POST /docs/apply-candidate          - promote candidate docs to effective docs
  POST /docs/discard-candidate        - discard pending candidate docs
  GET  /docs/status                   - JSON documentation (effective by default)
  GET  /docs/status.html              - interactive SPA dashboard
  GET  /docs/status.md                - Markdown documentation
  GET  /docs/section/{name}           - single documentation section
  GET  /docs/section/{name}/translate - translate one section on demand
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.dependencies import JobQueueDep
from app.models.docs import DocsCandidateReviewRequest, DocsRebuildRequest, DocsSection, DocsStatus
from app.services.docs_service import apply_docs_candidate, discard_docs_candidate, load_docs_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/docs", tags=["docs"])

_SPA_PATH = Path("static") / "status.html"


def _resolve_view(cached: DocsStatus, view: str) -> tuple[dict[str, DocsSection], datetime]:
    if view == "candidate":
        if not cached.candidate_sections:
            raise HTTPException(status_code=404, detail=f"Candidate docs not built for project '{cached.project}'.")
        return cached.candidate_sections, cached.candidate_generated_at or cached.generated_at
    return cached.sections, cached.generated_at


@router.post("/rebuild")
async def rebuild_docs(body: DocsRebuildRequest, queue: JobQueueDep) -> dict:
    """Trigger background documentation rebuild. Poll status at GET /tasks/{job_id}."""
    job_id = await queue.submit(
        "docs_rebuild",
        {
            "project": body.project,
            "force": body.force,
            "changed_component_ids": body.changed_component_ids,
            "changed_files": body.changed_files,
        },
    )
    return {"job_id": job_id, "status": "queued", "poll": f"/api/v1/tasks/{job_id}"}


@router.post("/apply-candidate", response_model=DocsStatus)
async def apply_candidate_docs(
    project: str = Query(default="mnemoforge", min_length=1, max_length=128),
    body: DocsCandidateReviewRequest | None = None,
    queue: JobQueueDep = None,
) -> DocsStatus:
    try:
        review = body or DocsCandidateReviewRequest()
        status = apply_docs_candidate(
            project,
            reviewed_by=review.reviewed_by,
            review_source=review.review_source,
            reason=review.reason,
        )
        if queue is not None:
            await queue.submit("docs_sync_memory", {"project": project})
        return status
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404, detail=detail) from exc


@router.post("/discard-candidate", response_model=DocsStatus)
async def discard_candidate_docs(
    project: str = Query(default="mnemoforge", min_length=1, max_length=128),
    body: DocsCandidateReviewRequest | None = None,
) -> DocsStatus:
    try:
        review = body or DocsCandidateReviewRequest()
        return discard_docs_candidate(
            project,
            reviewed_by=review.reviewed_by,
            review_source=review.review_source,
            reason=review.reason,
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404, detail=detail) from exc


@router.get("/status", response_model=DocsStatus)
async def get_docs_status(
    project: str = Query(default="mnemoforge", min_length=1, max_length=128),
    view: str = Query(default="effective", pattern="^(effective|candidate)$"),
) -> DocsStatus:
    """Return cached JSON documentation. 404 if not yet built; call POST /docs/rebuild first."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail=f"Docs not built for project '{project}'. Call POST /docs/rebuild first.",
        )
    sections, generated_at = _resolve_view(cached, view)
    return DocsStatus(
        project=cached.project,
        generated_at=generated_at,
        sections=sections,
        stale=cached.stale,
        stale_reason=cached.stale_reason,
        candidate_generated_at=cached.candidate_generated_at,
        candidate_sections=cached.candidate_sections,
        last_review_action=cached.last_review_action,
        last_reviewed_by=cached.last_reviewed_by,
        last_review_source=cached.last_review_source,
        last_reviewed_at=cached.last_reviewed_at,
        last_review_reason=cached.last_review_reason,
    )


@router.get("/status.html", response_class=HTMLResponse)
async def get_docs_html() -> HTMLResponse:
    """Serve the interactive SPA dashboard with embedded API key, matching dashboard auth flow."""
    if not _SPA_PATH.exists():
        raise HTTPException(status_code=503, detail="Dashboard not deployed (static/status.html missing)")
    from app.config import settings

    html = _SPA_PATH.read_text(encoding="utf-8").replace("__API_KEY__", settings.api_key or "")
    return HTMLResponse(content=html)


@router.get("/status.md", response_class=PlainTextResponse)
async def get_docs_markdown(
    project: str = Query(default="mnemoforge", min_length=1, max_length=128),
    view: str = Query(default="effective", pattern="^(effective|candidate)$"),
) -> PlainTextResponse:
    """Return documentation as Markdown."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(status_code=404, detail=f"Docs not built for project '{project}'.")
    sections, generated_at = _resolve_view(cached, view)

    lines = [f"# {project} - Project Documentation", f"_Generated: {generated_at.isoformat()}_", ""]
    if cached.stale:
        stale_reason = f" ({cached.stale_reason})" if cached.stale_reason else ""
        lines += [f"_Projection is stale{stale_reason}._", ""]
    section_order = [
        "overview",
        "architecture",
        "laws",
        "features",
        "pending",
        "runtime_hints",
        "tasks",
        "decisions",
        "skills",
        "performance",
    ]
    for key in section_order:
        section = sections.get(key)
        if section:
            lines += [f"## {section.name}", "", section.content, ""]
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")


@router.get("/section/{name}/translate")
async def translate_section(
    name: str,
    project: str = Query(default="mnemoforge", min_length=1, max_length=128),
    view: str = Query(default="effective", pattern="^(effective|candidate)$"),
) -> dict:
    """Translate a documentation section on demand without changing cached docs."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(status_code=404, detail=f"Docs not built for project '{project}'.")
    sections, _ = _resolve_view(cached, view)
    section = sections.get(name)
    if not section:
        raise HTTPException(status_code=404, detail=f"Section '{name}' not found.")
    from app.config import settings
    from app.services.project_tree_doc import translate_doc

    try:
        translated = await translate_doc(section.content, settings.glm_response_language)
    except RuntimeError as exc:
        detail = str(exc).strip() or "Translation failed"
        status_code = 503 if "no cloud llm configured" in detail.lower() else 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {
        "project": project,
        "section": name,
        "language": settings.glm_response_language,
        "original": section.content,
        "translated": translated,
    }


@router.get("/section/{name}", response_model=DocsSection)
async def get_section(
    name: str,
    project: str = Query(default="mnemoforge", min_length=1, max_length=128),
    view: str = Query(default="effective", pattern="^(effective|candidate)$"),
) -> DocsSection:
    """Return a single documentation section by name."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(status_code=404, detail=f"Docs not built for project '{project}'.")
    sections, _ = _resolve_view(cached, view)
    section = sections.get(name)
    if not section:
        raise HTTPException(status_code=404, detail=f"Section '{name}' not found.")
    return section
