"""
Semantic Adaptation Layer — glossary management and text normalization.

POST /normalization/normalize          — normalize text using agent glossary
POST /normalization/terms              — add a glossary term
GET  /normalization/terms              — list agent's glossary terms
DELETE /normalization/terms/{id}       — delete a glossary term
POST /normalization/feedback           — record whether normalization was correct;
                                         auto-updates glossary on correction
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep
from app.services.normalization_service import (
    AGENT_ID_GLOBAL,
    CATEGORY,
    _norm_svc,
)
from app.services.embedding_gateway import embed_text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/normalization", tags=["normalization"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class NormalizeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)
    agent_id: str = Field(..., min_length=1, max_length=256)


class NormalizeResponse(BaseModel):
    original: str
    normalized: str
    was_changed: bool
    applied: list[dict]


class AddTermRequest(BaseModel):
    term: str = Field(..., min_length=1, max_length=256, description="Term to recognize")
    expansion: str = Field(..., min_length=1, max_length=1024, description="Replacement text")
    agent_id: str = Field(..., min_length=1, max_length=256)
    global_scope: bool = Field(False, description="Apply to all agents (shared glossary)")


class TermRecord(BaseModel):
    id: UUID
    term: str
    expansion: str
    agent_id: str


class DeleteTermResponse(BaseModel):
    deleted: bool


class NormalizationFeedbackRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=256)
    term: str = Field(..., min_length=1, max_length=256, description="Term that was normalized")
    was_helpful: bool = Field(..., description="True if the normalization was correct")
    corrected_expansion: str | None = Field(
        None, description="Correct expansion when was_helpful=False"
    )


class NormalizationFeedbackResponse(BaseModel):
    feedback_recorded: bool
    term_updated: bool
    new_expansion: str | None = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/normalize", response_model=NormalizeResponse)
async def normalize_text(body: NormalizeRequest, qdrant: QdrantDep):
    """Normalize text by applying the agent's glossary substitutions."""
    result = await _norm_svc.normalize(body.text, body.agent_id, qdrant)
    return NormalizeResponse(
        original=result.original,
        normalized=result.normalized,
        was_changed=result.was_changed,
        applied=result.applied,
    )


@router.post("/terms", status_code=201)
async def add_term(body: AddTermRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """Add a glossary term. Use global_scope=true for terms shared across all agents."""
    memory_id = await _norm_svc.add_term(
        term=body.term,
        expansion=body.expansion,
        agent_id=body.agent_id,
        qdrant=qdrant,
        ollama=ollama,
        global_scope=body.global_scope,
    )
    return {
        "id": str(memory_id),
        "term": body.term,
        "expansion": body.expansion,
        "scope": "global" if body.global_scope else body.agent_id,
    }


@router.get("/terms", response_model=list[TermRecord])
async def list_terms(
    qdrant: QdrantDep,
    agent_id: str = Query(..., min_length=1, max_length=256),
):
    """List all glossary terms for an agent (includes global terms)."""
    from qdrant_client.http import models as qmodels

    records: list[TermRecord] = []

    for aid in [agent_id, AGENT_ID_GLOBAL]:
        try:
            results, _ = await qdrant._client.scroll(
                collection_name=qdrant._collection,
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="agent_id", match=qmodels.MatchValue(value=aid)
                        ),
                        qmodels.FieldCondition(
                            key="category", match=qmodels.MatchValue(value=CATEGORY)
                        ),
                    ]
                ),
                limit=500,
                with_payload=True,
                with_vectors=False,
            )
            for point in results:
                tags = point.payload.get("tags", [])
                term = next(
                    (t[len("term:"):] for t in tags if t.startswith("term:")), None
                )
                expansion = next(
                    (t[len("expansion:"):] for t in tags if t.startswith("expansion:")),
                    None,
                )
                if term and expansion:
                    records.append(
                        TermRecord(
                            id=UUID(str(point.id)),
                            term=term,
                            expansion=expansion,
                            agent_id=aid,
                        )
                    )
        except Exception as e:
            logger.warning("Failed to list terms for agent '%s': %s", aid, e)

    return records


@router.post("/feedback", response_model=NormalizationFeedbackResponse)
async def normalization_feedback(
    body: NormalizationFeedbackRequest, qdrant: QdrantDep, ollama: OllamaDep
):
    """Record whether a normalization was helpful. When was_helpful=False and
    corrected_expansion is provided, the glossary term is automatically updated."""
    from qdrant_client.http import models as qmodels
    from app.models.enums import MemoryType
    from app.models.memory import MemoryCreate

    # Store feedback record
    feedback_content = (
        f"normalization_feedback term={body.term} "
        f"helpful={body.was_helpful}"
        + (f" corrected={body.corrected_expansion}" if body.corrected_expansion else "")
    )
    fb_vector, embedding_meta = await embed_text(
        feedback_content,
        primary=ollama,
        purpose="normalization_feedback",
        fallback_reason="normalization_feedback_embedding_unavailable",
    )
    fb_mem = MemoryCreate(
        content=feedback_content,
        agent_id=body.agent_id,
        memory_type=MemoryType.experience,
        category="normalization_feedback",
        importance_score=0.5 if body.was_helpful else 0.75,
        source="normalization-feedback",
        tags=[
            "normalization_feedback",
            f"term:{body.term.lower()}",
            f"helpful:{body.was_helpful}",
        ],
        meta=embedding_meta,
    )
    await qdrant.insert(fb_mem, fb_vector)

    term_updated = False
    new_expansion: str | None = None

    # Auto-correct: find old term, replace with corrected expansion
    if not body.was_helpful and body.corrected_expansion:
        try:
            results, _ = await qdrant._client.scroll(
                collection_name=qdrant._collection,
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="agent_id", match=qmodels.MatchValue(value=body.agent_id)
                        ),
                        qmodels.FieldCondition(
                            key="category", match=qmodels.MatchValue(value=CATEGORY)
                        ),
                        qmodels.FieldCondition(
                            key="tags",
                            match=qmodels.MatchValue(value=f"term:{body.term.lower()}"),
                        ),
                    ]
                ),
                limit=5,
                with_payload=True,
                with_vectors=False,
            )
            for point in results:
                await _norm_svc.delete_term(UUID(str(point.id)), body.agent_id, qdrant)

            await _norm_svc.add_term(
                term=body.term,
                expansion=body.corrected_expansion,
                agent_id=body.agent_id,
                qdrant=qdrant,
                ollama=ollama,
            )
            term_updated = True
            new_expansion = body.corrected_expansion
        except Exception as e:
            logger.warning("Failed to update term '%s': %s", body.term, e)

    return NormalizationFeedbackResponse(
        feedback_recorded=True,
        term_updated=term_updated,
        new_expansion=new_expansion,
    )


@router.delete("/terms/{memory_id}", response_model=DeleteTermResponse)
async def delete_term(
    memory_id: UUID,
    qdrant: QdrantDep,
    agent_id: str = Query(..., min_length=1, max_length=256),
):
    """Delete a glossary term by its memory ID."""
    try:
        await _norm_svc.delete_term(memory_id, agent_id, qdrant)
        return DeleteTermResponse(deleted=True)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
