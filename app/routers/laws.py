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
from app.models.rule_lifecycle import (
    RuleCandidateListResponse,
    RuleCandidateProjectionReport,
    RuleCandidateProjectionRequest,
    RuleCandidateReviewActionRequest,
    RuleCandidateReviewActionResponse,
    RuleCandidateReviewPacket,
    RuleCandidateReviewRequest,
    RuleCandidatePromoteRequest,
    RuleCandidatePromoteResponse,
    RuleCandidateReviseLawRequest,
    RuleCandidateReviseLawResponse,
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
from app.services.rule_lifecycle_service import (
    build_rule_candidate_review_packet,
    get_rule_lifecycle_store,
    promote_rule_candidate,
    project_rule_candidates_from_stenographer,
    revise_law_from_rule_candidate,
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


@router.post("/candidates/project-from-stenography", response_model=RuleCandidateProjectionReport)
async def project_rule_candidates(body: RuleCandidateProjectionRequest):
    try:
        return project_rule_candidates_from_stenographer(project=body.project, limit=body.limit)
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.get("/candidates", response_model=RuleCandidateListResponse)
async def list_rule_candidates(
    project: Optional[str] = Query(None, max_length=128),
    status: Optional[str] = Query(None, pattern="^(candidate|needs_clarification|trial|revision_pending|rejected|suppressed)$"),
    source_task_id: Optional[str] = Query(None, max_length=256),
    limit: int = Query(100, ge=1, le=500),
):
    store = get_rule_lifecycle_store()
    items = store.list_candidates(project=project, status=status, source_task_id=source_task_id, limit=limit)
    return RuleCandidateListResponse(total=len(items), items=items)


@router.post("/candidates/review-packet", response_model=RuleCandidateReviewPacket)
async def rule_candidate_review_packet(body: RuleCandidateReviewRequest, qdrant: QdrantDep):
    try:
        return await build_rule_candidate_review_packet(qdrant, body)
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.post("/candidates/{candidate_id}/review", response_model=RuleCandidateReviewActionResponse)
async def review_rule_candidate(candidate_id: str, body: RuleCandidateReviewActionRequest):
    try:
        store = get_rule_lifecycle_store()
        return store.review_candidate(
            candidate_id,
            action=body.action,
            reason=body.reason,
            acted_by=body.acted_by,
            source=body.source,
        )
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.post("/candidates/{candidate_id}/promote", response_model=RuleCandidatePromoteResponse)
async def promote_rule_candidate_endpoint(
    candidate_id: str,
    body: RuleCandidatePromoteRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
):
    try:
        return await promote_rule_candidate(qdrant, ollama, candidate_id, body)
    except Exception as exc:
        raise _law_http_error(exc) from exc


@router.post("/candidates/{candidate_id}/revise-law", response_model=RuleCandidateReviseLawResponse)
async def revise_law_from_rule_candidate_endpoint(
    candidate_id: str,
    body: RuleCandidateReviseLawRequest,
    qdrant: QdrantDep,
    ollama: OllamaDep,
):
    try:
        return await revise_law_from_rule_candidate(qdrant, ollama, candidate_id, body)
    except Exception as exc:
        raise _law_http_error(exc) from exc


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
