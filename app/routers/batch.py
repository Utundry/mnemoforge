import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Response, status

from app.dependencies import OllamaDep, QdrantDep
from app.models.memory import (
    BatchCreateRequest,
    BatchCreateResponse,
    CleanupRequest,
    CleanupResponse,
)
from app.services.embedding_gateway import embed_text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memories", tags=["batch"])


@router.post("/batch", response_model=BatchCreateResponse)
async def batch_create(body: BatchCreateRequest, qdrant: QdrantDep, ollama: OllamaDep, response: Response):
    created_ids: list[UUID] = []
    failed = 0
    for memory in body.memories:
        try:
            vector, embedding_meta = await embed_text(
                memory.content,
                primary=ollama,
                purpose="memory_batch_store",
                fallback_reason="memory_batch_store_embedding_unavailable",
            )
            memory.meta = {**(memory.meta or {}), **embedding_meta}
            mid = await qdrant.insert(memory, vector)
            created_ids.append(mid)
        except Exception as e:
            logger.warning("Batch insert failed for one item: %s", e)
            failed += 1

    if not created_ids:
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    elif failed:
        response.status_code = status.HTTP_207_MULTI_STATUS
    else:
        response.status_code = status.HTTP_201_CREATED

    return BatchCreateResponse(created_ids=created_ids, failed_count=failed)


@router.delete("/cleanup", response_model=CleanupResponse)
async def cleanup(body: CleanupRequest, qdrant: QdrantDep):
    deleted = await qdrant.delete_by_filter(
        agent_id=body.agent_id,
        min_importance=body.min_importance,
        max_age_days=body.max_age_days,
    )
    return CleanupResponse(deleted_count=deleted)
