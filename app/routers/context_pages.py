from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.services.context_page_store import (
    ContextPageIntegrityError,
    compact_page,
    get_context_page_store,
    index_payload_for_page,
)
from app.services.memory_store import get_memory_store
from app.services.project_identity_service import resolve_project_id
from app.services.project_tasks_store import get_project_tasks_store
from app.services.unified_artifact_service import get_unified_artifact_service

router = APIRouter(prefix="/context-pages", tags=["context-pages"])


class ContextPageCreateRequest(BaseModel):
    parent_ref: str
    project: str = "mnemoforge"
    page_kind: str = "entry"
    page_index: int = Field(1, ge=1)
    title: str = ""
    summary: str = ""
    content: str = ""
    status: str = "active"
    created_by: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextPageUpdateRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    updated_by: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextPageArchiveRequest(BaseModel):
    updated_by: str = ""
    reason: str = ""


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_context_page(body: ContextPageCreateRequest):
    await _ensure_parent_exists(body.parent_ref, body.project)
    try:
        page = get_context_page_store().create_page(
            parent_ref=body.parent_ref,
            project=body.project,
            page_kind=body.page_kind,
            page_index=body.page_index,
            title=body.title,
            summary=body.summary,
            content=body.content,
            status=body.status,
            created_by=body.created_by,
            metadata=body.metadata,
        )
    except ContextPageIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _page_response(page)


@router.get("/entry")
async def get_entry_page(
    parent_ref: str = Query(...),
    include_history: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
):
    packet = get_context_page_store().entry_packet(parent_ref=parent_ref, include_history=include_history, limit=limit)
    if not packet:
        raise HTTPException(status_code=404, detail="entry page not found")
    return packet


@router.get("/toc")
async def list_context_page_toc(
    parent_ref: str = Query(...),
    include_history: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
):
    pages = get_context_page_store().list_pages(parent_ref=parent_ref, include_history=include_history, limit=limit)
    return {
        "parent_ref": parent_ref,
        "items": [compact_page(page, include_content=False) for page in pages],
        "count": len(pages),
        "include_history": include_history,
    }


@router.get("/indexable")
async def list_indexable_context_pages(project: str | None = None, limit: int = Query(100, ge=1, le=500)):
    pages = get_context_page_store().ordinary_indexable_pages(project=project, limit=limit)
    return {
        "items": [index_payload_for_page(page) for page in pages],
        "count": len(pages),
        "source_of_truth": "sqlite",
        "index_role": "qdrant_derived_active_pages_only",
    }


@router.get("/{page_id}")
async def get_context_page(page_id: str, include_history: bool = Query(False), detail: str = Query("compact")):
    page = get_context_page_store().get_page(page_id=page_id, include_history=include_history)
    if not page:
        raise HTTPException(status_code=404, detail="page not found")
    return _page_response(page, include_content=detail == "full")


@router.patch("/{page_id}")
async def supersede_context_page(page_id: str, body: ContextPageUpdateRequest):
    try:
        page = get_context_page_store().supersede_page(
            page_id=page_id,
            title=body.title,
            summary=body.summary,
            content=body.content,
            updated_by=body.updated_by,
            metadata=body.metadata,
        )
    except ContextPageIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _page_response(page, include_content=True)


@router.post("/{page_id}/archive")
async def archive_context_page(page_id: str, body: ContextPageArchiveRequest):
    try:
        page = get_context_page_store().archive_page(page_id=page_id, updated_by=body.updated_by)
    except ContextPageIntegrityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _page_response(page, include_content=True)


def _page_response(page: dict[str, Any], *, include_content: bool = True) -> dict[str, Any]:
    data = compact_page(page, include_content=include_content)
    data["source_of_truth"] = "sqlite"
    data["index_role"] = "qdrant_derived"
    return data


async def _ensure_parent_exists(parent_ref: str, project: str) -> None:
    ref = str(parent_ref or "").strip()
    if not ref:
        raise HTTPException(status_code=422, detail="parent_ref is required")
    kind = ref.split(":", 1)[0]
    if kind in {"task", "improvement"}:
        canonical_project = resolve_project_id(project)
        try:
            artifact = await get_unified_artifact_service().get_artifact(ref)
            if resolve_project_id(str(artifact.project or "")) != canonical_project:
                raise HTTPException(status_code=409, detail="parent artifact belongs to another project")
            return
        except HTTPException:
            raise
        except Exception:
            if kind == "task":
                parts = ref.split(":", 2)
                if len(parts) == 3:
                    task = get_project_tasks_store().get_task_by_task_id(project=parts[1], task_id=parts[2])
                    if task:
                        return
            raise HTTPException(status_code=404, detail=f"parent artifact not found: {parent_ref}")
    if kind == "memory":
        parts = ref.split(":", 2)
        if len(parts) != 3:
            raise HTTPException(status_code=422, detail="memory parent_ref must be memory:<project>:<id>")
        row = await get_memory_store().get(parts[2])
        if not row:
            raise HTTPException(status_code=404, detail=f"parent memory not found: {parent_ref}")
        return
    raise HTTPException(status_code=422, detail=f"unsupported parent_ref kind for MVP: {kind}")
