from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Optional
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings

logger = logging.getLogger(__name__)

_ensure_lock: asyncio.Lock = asyncio.Lock()
# Cache is per AsyncQdrantClient instance. Use weak refs to avoid id() reuse bugs in tests.
_ensured: "weakref.WeakKeyDictionary[AsyncQdrantClient, set[str]]" = weakref.WeakKeyDictionary()


_PAYLOAD_INDEXES = [
    ("artifact_scope", qmodels.PayloadSchemaType.KEYWORD),
    ("status", qmodels.PayloadSchemaType.KEYWORD),
    ("action_type", qmodels.PayloadSchemaType.KEYWORD),
    ("updated_at", qmodels.PayloadSchemaType.FLOAT),
]


async def ensure_learning_collection(client: AsyncQdrantClient) -> None:
    """
    Ensure the learning artifacts collection exists and has required payload indexes.

    This is intentionally separate from the main memories collection to avoid mixing
    artifact vectors with user memories and to allow different filtering semantics.
    """
    name = settings.qdrant_learning_collection_name
    try:
        existing = _ensured.get(client)
        if existing and name in existing:
            return
    except Exception:
        # WeakKeyDictionary can fail if client isn't weakref-able; fall back to always ensuring.
        existing = None
    async with _ensure_lock:
        try:
            existing2 = _ensured.get(client)
            if existing2 and name in existing2:
                return
        except Exception:
            existing2 = None

        collections = await client.get_collections()
        names = {c.name for c in collections.collections}
        if name not in names:
            await client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=settings.embedding_dimensions,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            logger.info("Created learning collection '%s'", name)

        for field, schema in _PAYLOAD_INDEXES:
            try:
                await client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception as exc:
                logger.debug("Learning index payload '%s' already exists or failed: %s", field, exc)

        try:
            s = _ensured.get(client)
            if s is None:
                s = set()
                _ensured[client] = s
            s.add(name)
        except Exception:
            pass


async def upsert_artifact_vector(
    client: AsyncQdrantClient,
    *,
    artifact_id: UUID,
    vector: list[float],
    payload: dict,
) -> None:
    await ensure_learning_collection(client)
    await client.upsert(
        collection_name=settings.qdrant_learning_collection_name,
        points=[
            qmodels.PointStruct(
                id=str(artifact_id),
                vector=vector,
                payload=payload,
            )
        ],
    )


async def search_similar_candidates(
    client: AsyncQdrantClient,
    *,
    vector: list[float],
    action_type: str,
    limit: int = 5,
) -> list[tuple[UUID, float, dict]]:
    """
    Search learning artifact vectors for similar *candidate* rows of the same action_type.
    Returns (artifact_id, score, payload).
    """
    await ensure_learning_collection(client)
    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="action_type",
                match=qmodels.MatchValue(value=action_type),
            ),
            qmodels.FieldCondition(
                key="artifact_scope",
                match=qmodels.MatchValue(value="candidate"),
            ),
        ],
        must_not=[
            qmodels.FieldCondition(
                key="status",
                match=qmodels.MatchValue(value="archived"),
            ),
            qmodels.FieldCondition(
                key="status",
                match=qmodels.MatchValue(value="disabled"),
            ),
        ],
    )
    results = await client.search(
        collection_name=settings.qdrant_learning_collection_name,
        query_vector=vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )

    out: list[tuple[UUID, float, dict]] = []
    for r in results:
        try:
            uid = UUID(str(r.id))
        except Exception:
            continue
        out.append((uid, float(r.score or 0.0), dict(r.payload or {})))
    return out


async def set_artifact_payload(
    client: AsyncQdrantClient,
    *,
    artifact_id: UUID,
    payload: dict,
) -> None:
    """Best-effort payload update for an existing vector point."""
    await ensure_learning_collection(client)
    await client.set_payload(
        collection_name=settings.qdrant_learning_collection_name,
        payload=payload,
        points=[str(artifact_id)],
    )
