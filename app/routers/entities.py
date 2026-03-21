"""
Entity Profiles & Relations — first-class entity modeling inspired by Zep and Letta.

Adds structured entity profiles (user, agent, project) and lightweight relations
instead of relying solely on free text and tags.

Storage strategy (no schema change needed):
  Entities:   MemoryRecord, category="entity_profile", memory_type="profile"
  Relations:  MemoryRecord, category="entity_relation", memory_type="context"

POST /entities                          — create entity profile
GET  /entities                          — list entities for agent
GET  /entities/{id}                     — get entity by ID
PUT  /entities/{id}                     — update entity profile
DELETE /entities/{id}                   — delete entity

POST /entities/relations                — create relation between two entities
GET  /entities/{id}/relations           — list relations for an entity
DELETE /entities/relations/{id}         — delete a relation
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep
from app.models.enums import MemoryType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/entities", tags=["entities"])

ENTITY_CATEGORY = "entity_profile"
RELATION_CATEGORY = "entity_relation"


# ── Schemas ────────────────────────────────────────────────────────────────────

class EntityCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256, description="Entity name (e.g. 'Alice', 'project-alpha')")
    entity_type: str = Field(..., description="Type: user | agent | project | team | system")
    agent_id: str = Field(..., min_length=1, max_length=256, description="Owning agent namespace")
    description: str = Field("", max_length=5000, description="Profile description / known facts")
    attributes: dict = Field(default_factory=dict, description="Structured key-value attributes")
    tags: list[str] = Field(default_factory=list)
    importance_score: float = Field(0.8, ge=0.0, le=1.0)


class EntityRecord(BaseModel):
    id: str
    name: str
    entity_type: str
    agent_id: str
    description: str
    attributes: dict
    tags: list[str]
    importance_score: float
    created_at: str


class EntityUpdate(BaseModel):
    description: Optional[str] = Field(None, max_length=5000)
    attributes: Optional[dict] = None
    tags: Optional[list[str]] = None
    importance_score: Optional[float] = Field(None, ge=0.0, le=1.0)


class RelationCreate(BaseModel):
    from_entity_id: str = Field(..., description="Source entity ID")
    to_entity_id: str = Field(..., description="Target entity ID")
    relation_type: str = Field(..., description="Relation type: owns | uses | knows | works_on | reports_to")
    agent_id: str = Field(..., min_length=1, max_length=256)
    description: str = Field("", max_length=1000)
    strength: float = Field(0.7, ge=0.0, le=1.0, description="Relation strength/confidence")


class RelationRecord(BaseModel):
    id: str
    from_entity_id: str
    to_entity_id: str
    relation_type: str
    agent_id: str
    description: str
    strength: float
    created_at: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _entity_content(entity: EntityCreate) -> str:
    """Serialize entity to searchable text content."""
    parts = [f"{entity.entity_type.upper()}: {entity.name}"]
    if entity.description:
        parts.append(entity.description)
    for k, v in entity.attributes.items():
        parts.append(f"{k}: {v}")
    return "\n".join(parts)


def _point_to_entity(point) -> EntityRecord:
    p = point.payload
    import json
    attrs_raw = p.get("entity_attributes", "{}")
    try:
        attrs = json.loads(attrs_raw) if isinstance(attrs_raw, str) else attrs_raw
    except Exception:
        attrs = {}
    return EntityRecord(
        id=str(point.id),
        name=p.get("entity_name", ""),
        entity_type=p.get("entity_type", "unknown"),
        agent_id=p.get("agent_id", ""),
        description=p.get("content", ""),
        attributes=attrs,
        tags=p.get("tags", []),
        importance_score=p.get("importance_score", 0.8),
        created_at=p.get("timestamp", ""),
    )


def _point_to_relation(point) -> RelationRecord:
    p = point.payload
    tags = p.get("tags", [])
    from_id = next((t[len("from:"):] for t in tags if t.startswith("from:")), "")
    to_id = next((t[len("to_entity:"):] for t in tags if t.startswith("to_entity:")), "")
    rel_type = next((t[len("rel:"):] for t in tags if t.startswith("rel:")), "")
    return RelationRecord(
        id=str(point.id),
        from_entity_id=from_id,
        to_entity_id=to_id,
        relation_type=rel_type,
        agent_id=p.get("agent_id", ""),
        description=p.get("content", ""),
        strength=p.get("importance_score", 0.7),
        created_at=p.get("timestamp", ""),
    )


# ── Entity CRUD ────────────────────────────────────────────────────────────────

@router.post("", status_code=201, response_model=EntityRecord)
async def create_entity(body: EntityCreate, qdrant: QdrantDep, ollama: OllamaDep):
    """Create a new entity profile."""
    import json
    from app.models.memory import MemoryCreate

    content = _entity_content(body)
    vector = await ollama.embed(content)

    mem = MemoryCreate(
        content=content,
        agent_id=body.agent_id,
        memory_type=MemoryType.profile,
        category=ENTITY_CATEGORY,
        importance_score=body.importance_score,
        source="entities",
        tags=["entity", f"type:{body.entity_type}", f"name:{body.name}"] + body.tags,
        decay_rate=0.0,  # entity profiles are permanent
    )
    memory_id = await qdrant.insert(mem, vector)

    # Store structured attributes as extra payload fields
    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload={
            "entity_name": body.name,
            "entity_type": body.entity_type,
            "entity_attributes": json.dumps(body.attributes),
        },
        points=[str(memory_id)],
    )

    record = await qdrant.get(memory_id)
    return EntityRecord(
        id=str(record.id),
        name=body.name,
        entity_type=body.entity_type,
        agent_id=body.agent_id,
        description=record.content,
        attributes=body.attributes,
        tags=record.tags,
        importance_score=record.importance_score,
        created_at=record.timestamp.isoformat(),
    )


@router.get("", response_model=list[EntityRecord])
async def list_entities(
    qdrant: QdrantDep,
    agent_id: str = Query(..., min_length=1),
    entity_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List entity profiles for an agent."""
    from qdrant_client.http import models as qmodels

    # Prefer a narrow server-side filter, then defensively re-filter in Python.
    # This protects the endpoint from payload-filter inconsistencies and keeps
    # create->list flows stable immediately after writes.
    must = [
        qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=agent_id)),
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=ENTITY_CATEGORY)),
    ]

    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=max(limit * 3, limit),
        with_payload=True,
        with_vectors=False,
    )

    filtered = []
    expected_type_tag = f"type:{entity_type}" if entity_type else None
    for point in results:
        payload = point.payload or {}
        if payload.get("agent_id") != agent_id:
            continue
        if payload.get("category") != ENTITY_CATEGORY:
            continue
        if expected_type_tag and expected_type_tag not in payload.get("tags", []):
            continue
        filtered.append(point)
        if len(filtered) >= limit:
            break

    return [_point_to_entity(p) for p in filtered]


@router.get("/{entity_id}", response_model=EntityRecord)
async def get_entity(entity_id: UUID, qdrant: QdrantDep):
    """Get a single entity profile."""
    try:
        record = await qdrant.get(entity_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Entity {entity_id} not found")

    from qdrant_client.http import models as qmodels
    results = await qdrant._client.retrieve(
        collection_name=qdrant._collection,
        ids=[str(entity_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise HTTPException(status_code=404, detail=f"Entity {entity_id} not found")
    return _point_to_entity(results[0])


@router.put("/{entity_id}", response_model=EntityRecord)
async def update_entity(entity_id: UUID, body: EntityUpdate, qdrant: QdrantDep, ollama: OllamaDep):
    """Update an entity profile's description, attributes, or tags."""
    import json

    # Verify exists
    results = await qdrant._client.retrieve(
        collection_name=qdrant._collection,
        ids=[str(entity_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise HTTPException(status_code=404, detail=f"Entity {entity_id} not found")

    patch: dict = {}
    if body.description is not None:
        patch["content"] = body.description
    if body.attributes is not None:
        patch["entity_attributes"] = json.dumps(body.attributes)
    if body.tags is not None:
        patch["tags"] = body.tags
    if body.importance_score is not None:
        patch["importance_score"] = body.importance_score

    if patch:
        await qdrant._client.set_payload(
            collection_name=qdrant._collection,
            payload=patch,
            points=[str(entity_id)],
        )

        # Re-embed if content changed
        if body.description is not None:
            new_vector = await ollama.embed(body.description)
            from qdrant_client.http import models as qmodels
            await qdrant._client.update_vectors(
                collection_name=qdrant._collection,
                points=[qmodels.PointVectors(id=str(entity_id), vector=new_vector)],
            )

    updated = await qdrant._client.retrieve(
        collection_name=qdrant._collection,
        ids=[str(entity_id)],
        with_payload=True,
        with_vectors=False,
    )
    return _point_to_entity(updated[0])


@router.delete("/{entity_id}", status_code=204)
async def delete_entity(entity_id: UUID, qdrant: QdrantDep):
    """Delete an entity profile."""
    await qdrant.delete(entity_id)


# ── Relations ──────────────────────────────────────────────────────────────────

@router.post("/relations", status_code=201, response_model=RelationRecord)
async def create_relation(body: RelationCreate, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Create a directed relation between two entities.
    Example: user-alice --[uses]--> project-supermemory
    """
    from app.models.memory import MemoryCreate

    content = (
        f"{body.from_entity_id} --[{body.relation_type}]--> {body.to_entity_id}"
        + (f": {body.description}" if body.description else "")
    )
    vector = await ollama.embed(content)

    mem = MemoryCreate(
        content=content,
        agent_id=body.agent_id,
        memory_type=MemoryType.context,
        category=RELATION_CATEGORY,
        importance_score=body.strength,
        source="entities",
        tags=[
            "relation",
            f"from:{body.from_entity_id}",
            f"to_entity:{body.to_entity_id}",
            f"rel:{body.relation_type}",
        ],
        decay_rate=0.5,
    )
    memory_id = await qdrant.insert(mem, vector)
    record = await qdrant.get(memory_id)

    return RelationRecord(
        id=str(record.id),
        from_entity_id=body.from_entity_id,
        to_entity_id=body.to_entity_id,
        relation_type=body.relation_type,
        agent_id=body.agent_id,
        description=body.description,
        strength=body.strength,
        created_at=record.timestamp.isoformat(),
    )


@router.get("/{entity_id}/relations", response_model=list[RelationRecord])
async def list_relations(
    entity_id: str,
    qdrant: QdrantDep,
    direction: str = Query("both", pattern="^(from|to|both)$"),
    limit: int = Query(50, ge=1, le=200),
):
    """List relations for an entity. direction: from | to | both."""
    from qdrant_client.http import models as qmodels

    results: list = []

    if direction in ("from", "both"):
        r, _ = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=RELATION_CATEGORY)),
                qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"from:{entity_id}")),
            ]),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        results.extend(r)

    if direction in ("to", "both"):
        r, _ = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qmodels.Filter(must=[
                qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=RELATION_CATEGORY)),
                qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"to_entity:{entity_id}")),
            ]),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        results.extend(r)

    return [_point_to_relation(p) for p in results]


@router.delete("/relations/{relation_id}", status_code=204)
async def delete_relation(relation_id: UUID, qdrant: QdrantDep):
    """Delete a relation."""
    await qdrant.delete(relation_id)
