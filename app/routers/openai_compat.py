"""
OpenAI-compatible integration adapter — inspired by Mem0/OpenMemory.

Provides an OpenAI-style API surface so agents that expect OpenAI memory APIs
can use SloplessCode without code changes.

POST /v1/memories         — add a memory (OpenAI-style)
GET  /v1/memories         — list memories for a user
DELETE /v1/memories/{id}  — delete a memory
POST /v1/memories/search  — search memories (OpenAI-style)

Maps to the native SloplessCode API internally.
Mounted at root (no /api/v1 prefix) for maximum compatibility.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep, ScorerDep
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.services.embedding_gateway import embed_query, embed_text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/memories", tags=["openai-compat"])


# ── OpenAI-style schemas ───────────────────────────────────────────────────────

class OAIAddMemoryRequest(BaseModel):
    messages: Optional[list[dict]] = None  # [{"role": "user", "content": "..."}]
    text: Optional[str] = None             # direct text alternative
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    metadata: Optional[dict] = None
    infer: bool = Field(True, description="If true, extract facts via LLM. If false, store as-is.")


class OAIMemoryItem(BaseModel):
    id: str
    memory: str           # OpenAI calls the content field "memory"
    user_id: Optional[str]
    agent_id: Optional[str]
    created_at: str
    updated_at: str
    score: Optional[float] = None  # present in search results


class OAIAddMemoryResponse(BaseModel):
    results: list[OAIMemoryItem]
    relations: list = Field(default_factory=list)  # future: entity relations


class OAISearchRequest(BaseModel):
    query: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    limit: int = Field(10, ge=1, le=50)


class OAISearchResponse(BaseModel):
    results: list[OAIMemoryItem]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_text(body: OAIAddMemoryRequest) -> str:
    """Extract text content from messages or direct text field."""
    if body.text:
        return body.text
    if body.messages:
        return "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}"
            for m in body.messages
            if m.get("content")
        )
    return ""


def _effective_agent(body) -> str:
    return body.agent_id or body.user_id or "default"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("", response_model=OAIAddMemoryResponse, status_code=201)
async def oai_add_memory(body: OAIAddMemoryRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Add memory in OpenAI Mem0-compatible format.

    Accepts either `messages` (conversation array) or `text` (direct string).
    If infer=true (default), extracts structured memories via LLM.
    If infer=false, stores the raw text as a single memory.
    """
    text = _extract_text(body)
    if not text.strip():
        return OAIAddMemoryResponse(results=[])

    agent_id = _effective_agent(body)
    stored: list[OAIMemoryItem] = []

    if body.infer:
        # Delegate to auto-extract via direct function call (no self-HTTP roundtrip)
        try:
            from app.routers.auto_memory import auto_extract, ExtractRequest as _ExtractReq
            extract_result = await auto_extract(
                _ExtractReq(text=text, agent_id=agent_id),
                qdrant,
                ollama,
            )
            for mem_data in extract_result.memories:
                vector, embedding_meta = await embed_text(
                    mem_data.content,
                    primary=ollama,
                    purpose="openai_compat_inferred_memory",
                    fallback_reason="openai_compat_inferred_memory_embedding_unavailable",
                )
                mem = MemoryCreate(
                    content=mem_data.content,
                    agent_id=agent_id,
                    memory_type=MemoryType.fact,
                    importance_score=mem_data.importance,
                    tags=mem_data.tags,
                    source="openai-compat",
                    meta=embedding_meta,
                )
                mid = await qdrant.insert(mem, vector)
                record = await qdrant.get(mid)
                stored.append(OAIMemoryItem(
                    id=str(record.id),
                    memory=record.content,
                    user_id=body.user_id,
                    agent_id=body.agent_id,
                    created_at=record.timestamp.isoformat(),
                    updated_at=record.timestamp.isoformat(),
                ))
        except Exception as e:
            logger.warning("OAI infer extraction failed, falling back to direct store: %s", e)
            body.infer = False  # fall through to direct store

    if not body.infer and not stored:
        # Direct store — no LLM extraction
        vector, embedding_meta = await embed_text(
            text,
            primary=ollama,
            purpose="openai_compat_memory",
            fallback_reason="openai_compat_memory_embedding_unavailable",
        )
        mem = MemoryCreate(
            content=text,
            agent_id=agent_id,
            memory_type=MemoryType.fact,
            importance_score=0.6,
            source="openai-compat",
            meta=embedding_meta,
        )
        mid = await qdrant.insert(mem, vector)
        record = await qdrant.get(mid)
        stored.append(OAIMemoryItem(
            id=str(record.id),
            memory=record.content,
            user_id=body.user_id,
            agent_id=body.agent_id,
            created_at=record.timestamp.isoformat(),
            updated_at=record.timestamp.isoformat(),
        ))

    return OAIAddMemoryResponse(results=stored)


@router.get("", response_model=list[OAIMemoryItem])
async def oai_list_memories(
    qdrant: QdrantDep,
    user_id: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List memories for a user or agent (OpenAI-compatible format)."""
    from qdrant_client.http import models as qmodels

    effective_agent = agent_id or user_id
    if not effective_agent:
        return []

    must = [qmodels.FieldCondition(
        key="agent_id", match=qmodels.MatchValue(value=effective_agent)
    )]
    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return [
        OAIMemoryItem(
            id=str(p.id),
            memory=p.payload.get("content", ""),
            user_id=user_id,
            agent_id=agent_id,
            created_at=p.payload.get("timestamp", ""),
            updated_at=p.payload.get("timestamp", ""),
        )
        for p in results
    ]


@router.post("/search", response_model=OAISearchResponse)
async def oai_search_memories(
    body: OAISearchRequest, qdrant: QdrantDep, ollama: OllamaDep, scorer: ScorerDep
):
    """Search memories (OpenAI Mem0-compatible format)."""
    from app.models.memory import SearchRequest

    effective_agent = body.agent_id or body.user_id

    vector, _embedding_meta = await embed_query(
        body.query,
        primary=ollama,
        purpose="openai_compat_search",
    )
    raw = await qdrant.search(
        vector=vector,
        agent_id=effective_agent,
        limit=body.limit,
    )
    ranked = scorer.rank(raw, limit=body.limit)

    return OAISearchResponse(results=[
        OAIMemoryItem(
            id=str(r.memory.id),
            memory=r.memory.content,
            user_id=body.user_id,
            agent_id=body.agent_id,
            created_at=r.memory.timestamp.isoformat(),
            updated_at=r.memory.timestamp.isoformat(),
            score=round(r.score, 4),
        )
        for r in ranked
    ])


@router.delete("/{memory_id}", status_code=204)
async def oai_delete_memory(memory_id: UUID, qdrant: QdrantDep):
    """Delete a memory by ID (OpenAI-compatible format)."""
    await qdrant.delete(memory_id)
