from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.dependencies import OllamaDep, QdrantDep
from app.models.law import (
    LAW_SCOPE_PATTERN,
    ProjectLawConfirmRequest,
    ProjectLawCreate,
    ProjectLawImportRequest,
    ProjectLawImportResponse,
    ProjectLawListResponse,
    ProjectLawRecord,
    ProjectLawStatusUpdate,
    ProjectLawUpdate,
)
from app.services.law_import_service import import_project_laws_from_markdown
from app.services.law_service import (
    confirm_project_law,
    create_project_law,
    get_project_law,
    list_project_laws,
    update_project_law,
    update_project_law_status,
)

router = APIRouter(prefix="/laws", tags=["laws"])


def _law_http_error(exc: Exception) -> HTTPException:
    status_code = 404 if str(exc) == "Law not found" else 400
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectLawRecord)
async def create_law(body: ProjectLawCreate, qdrant: QdrantDep, ollama: OllamaDep):
    try:
        return await create_project_law(qdrant, ollama, body)
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.post("/import-markdown", response_model=ProjectLawImportResponse)
async def import_laws_markdown(body: ProjectLawImportRequest, qdrant: QdrantDep, ollama: OllamaDep):
    try:
        return await import_project_laws_from_markdown(
            qdrant=qdrant,
            ollama=ollama,
            project=body.project,
            path=body.path,
            agent_id=body.agent_id,
            confirmed_by=body.confirmed_by,
            confirmation_source=body.confirmation_source,
            reason=body.reason,
            extra_tags=body.tags,
        )
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.get("", response_model=ProjectLawListResponse)
async def list_laws(
    qdrant: QdrantDep,
    project: Optional[str] = Query(None, max_length=128),
    status: str = Query("active", pattern="^(observed|proposed|reviewed|user_confirmed|active|suppressed|superseded|archived|all)$"),
    scope: Optional[str] = Query(None, pattern=LAW_SCOPE_PATTERN),
    include_promoted: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
):
    items = await list_project_laws(
        qdrant,
        project=project,
        status=status,
        scope=scope,
        include_promoted=include_promoted,
        limit=limit,
    )
    return ProjectLawListResponse(total=len(items), items=items)


@router.get("/{law_id}", response_model=ProjectLawRecord)
async def get_law(law_id: str, qdrant: QdrantDep):
    try:
        return await get_project_law(qdrant, law_id)
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.patch("/{law_id}", response_model=ProjectLawRecord)
async def patch_law(law_id: str, body: ProjectLawUpdate, qdrant: QdrantDep, ollama: OllamaDep):
    try:
        return await update_project_law(qdrant, ollama, law_id, body)
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.patch("/{law_id}/status", response_model=ProjectLawRecord)
async def patch_law_status(law_id: str, body: ProjectLawStatusUpdate, qdrant: QdrantDep):
    try:
        return await update_project_law_status(
            qdrant,
            law_id,
            status=body.status,
            reason=body.reason,
            acted_by=body.acted_by,
            action_source=body.action_source,
        )
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.post("/{law_id}/confirm", response_model=ProjectLawRecord)
async def confirm_law(law_id: str, body: ProjectLawConfirmRequest, qdrant: QdrantDep, ollama: OllamaDep):
    try:
        return await confirm_project_law(qdrant, ollama, law_id, body)
    except Exception as exc:
        raise _law_http_error(exc) from exc
