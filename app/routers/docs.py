"""
Living Documentation API.

Endpoints:
  POST /docs/rebuild          — trigger background docs rebuild
  GET  /docs/status           — JSON documentation (LLM-ready, from cache)
  GET  /docs/status.html      — interactive SPA dashboard
  GET  /docs/status.md        — Markdown documentation
  GET  /docs/section/{name}   — single section
  GET  /docs/section/{name}/translate — translate one section on demand
"""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.dependencies import JobQueueDep
from app.models.docs import DocsRebuildRequest, DocsSection, DocsStatus
from app.services.docs_service import load_docs_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/docs", tags=["docs"])

_SPA_PATH = Path("static") / "status.html"


@router.post("/rebuild")
async def rebuild_docs(body: DocsRebuildRequest, queue: JobQueueDep) -> dict:
    """Trigger background documentation rebuild. Poll status at GET /tasks/{job_id}."""
    job_id = await queue.submit("docs_rebuild", {"project": body.project, "force": body.force})
    return {"job_id": job_id, "status": "queued", "poll": f"/api/v1/tasks/{job_id}"}


@router.get("/status", response_model=DocsStatus)
async def get_docs_status(
    project: str = Query(
        default="supermemory",
        min_length=1,
        max_length=128,
    )
) -> DocsStatus:
    """Return cached JSON documentation. 404 if not yet built — call POST /docs/rebuild first."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail=f"Docs not built for project '{project}'. Call POST /docs/rebuild first.",
        )
    return cached


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
    project: str = Query(
        default="supermemory",
        min_length=1,
        max_length=128,
    )
) -> PlainTextResponse:
    """Return documentation as Markdown."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(status_code=404, detail=f"Docs not built for project '{project}'.")

    lines = [f"# {project} — Project Documentation", f"_Generated: {cached.generated_at.isoformat()}_", ""]
    section_order = ["overview", "architecture", "features", "pending", "decisions", "skills", "performance"]
    for key in section_order:
        section = cached.sections.get(key)
        if section:
            lines += [f"## {section.name}", "", section.content, ""]
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")


@router.get("/section/{name}/translate")
async def translate_section(
    name: str,
    project: str = Query(
        default="supermemory",
        min_length=1,
        max_length=128,
    ),
) -> dict:
    """Translate a documentation section on demand without changing cached docs."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(status_code=404, detail=f"Docs not built for project '{project}'.")
    section = cached.sections.get(name)
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
    project: str = Query(
        default="supermemory",
        min_length=1,
        max_length=128,
    ),
) -> DocsSection:
    """Return a single documentation section by name."""
    cached = load_docs_cache(project)
    if not cached:
        raise HTTPException(status_code=404, detail=f"Docs not built for project '{project}'.")
    section = cached.sections.get(name)
    if not section:
        raise HTTPException(status_code=404, detail=f"Section '{name}' not found.")
    return section
