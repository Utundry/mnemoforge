import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings
from app.core.exceptions import MemoryNotFoundError, QdrantServiceError, VectorDimensionMismatchError
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate, MemoryRecord, MemoryUpdate

logger = logging.getLogger(__name__)

# Project activity tracker — maps project name → last active timestamp (use-events only).
# In-memory: resets on server restart, but decay job runs every 24h so this is acceptable.
# Key insight: decay gate only needs to know "was project active today?", not exact history.
_project_activity: dict[str, float] = {}


def _update_project_activity(project: str, ts: float) -> None:
    _project_activity[project] = max(_project_activity.get(project, 0.0), ts)


def get_project_last_active(project: str) -> Optional[float]:
    return _project_activity.get(project)


def _validate_vector(vector: list[float]) -> None:
    from app.config import settings
    expected = settings.embedding_dimensions
    if len(vector) != expected:
        raise VectorDimensionMismatchError(
            f"Vector dimension mismatch: got {len(vector)}, expected {expected}. "
            f"Check EMBEDDING_DIMENSIONS in .env matches the embedding model output."
        )


PAYLOAD_INDEXES = [
    ("agent_id", qmodels.PayloadSchemaType.KEYWORD),
    ("memory_type", qmodels.PayloadSchemaType.KEYWORD),
    ("category", qmodels.PayloadSchemaType.KEYWORD),
    ("importance_score", qmodels.PayloadSchemaType.FLOAT),
    ("timestamp", qmodels.PayloadSchemaType.DATETIME),
    ("status", qmodels.PayloadSchemaType.KEYWORD),
    ("tags", qmodels.PayloadSchemaType.KEYWORD),
    ("last_access_ts", qmodels.PayloadSchemaType.DATETIME),
    ("last_decay_ts", qmodels.PayloadSchemaType.DATETIME),
    ("pinned", qmodels.PayloadSchemaType.KEYWORD),
    ("project", qmodels.PayloadSchemaType.KEYWORD),
    ("expires_at", qmodels.PayloadSchemaType.DATETIME),
    ("topic_path", qmodels.PayloadSchemaType.KEYWORD),
    ("scope", qmodels.PayloadSchemaType.KEYWORD),
    ("canonical_id", qmodels.PayloadSchemaType.KEYWORD),
]


class QdrantService:
    def __init__(self, client: AsyncQdrantClient):
        self._client = client
        self._collection = settings.qdrant_collection_name

    async def ensure_collection(self) -> None:
        collections = await self._client.get_collections()
        names = [c.name for c in collections.collections]
        if self._collection not in names:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=settings.embedding_dimensions,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            logger.info("Created collection '%s'", self._collection)
        else:
            info = await self._client.get_collection(collection_name=self._collection)
            config = info.config.params.vectors
            actual_size: Optional[int] = None
            if isinstance(config, dict):
                default_cfg = config.get("")
                if default_cfg is not None:
                    actual_size = getattr(default_cfg, "size", None)
            else:
                actual_size = getattr(config, "size", None)
            if actual_size is not None and actual_size != settings.embedding_dimensions:
                raise QdrantServiceError(
                    f"Qdrant collection '{self._collection}' expects {actual_size}-dim vectors, "
                    f"but EMBEDDING_DIMENSIONS={settings.embedding_dimensions}. "
                    "Update the config or recreate the collection."
                )
            logger.info("Collection '%s' already exists", self._collection)
        # Ensure all indexes exist (idempotent — safe to call on new or existing collection)
        for field, schema in PAYLOAD_INDEXES:
            try:
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=schema,
                )
                logger.debug("Ensured payload index on '%s'", field)
            except Exception as exc:
                logger.debug("Index '%s' already exists or failed: %s", field, exc)

    @staticmethod
    def _content_id(agent_id: str, source: str, content: str) -> UUID:
        """Deterministic UUID based on agent+source+content — prevents duplicates on re-ingest."""
        key = f"{agent_id}\x00{source}\x00{content}"
        return UUID(hashlib.md5(key.encode()).hexdigest())

    async def insert(self, memory: MemoryCreate, vector: list[float]) -> UUID:
        _validate_vector(vector)
        # Use deterministic ID for client-scan / watcher sources to deduplicate re-ingests
        if memory.source.startswith(("client-scan:", "watcher:")):
            memory_id = self._content_id(memory.agent_id, memory.source, memory.content)
        else:
            memory_id = uuid4()
        now = datetime.now(timezone.utc)
        payload = {
            "content": memory.content,
            "agent_id": memory.agent_id,
            "memory_type": memory.memory_type.value,
            "category": memory.category,
            "importance_score": memory.importance_score,
            "timestamp": now.isoformat(),
            "source": memory.source,
            "tags": memory.tags,
            "access_count": 0,
            "session_id": memory.session_id,
            "decay_rate": memory.effective_decay_rate,
            "pinned": memory.pinned,
            "last_access_ts": None,       # updated only on USE events, not peek
            "last_decay_ts": None,        # set on first decay job run
            "related_ids": memory.related_ids,
            "project": memory.project,
            "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
            "topic_path": memory.topic_path,
            "scope": memory.scope,
            "supports": memory.supports,
            "canonical_id": memory.canonical_id,
        }
        await self._client.upsert(
            collection_name=self._collection,
            points=[qmodels.PointStruct(id=str(memory_id), vector=vector, payload=payload)],
        )
        return memory_id

    async def get(self, memory_id: UUID) -> MemoryRecord:
        results = await self._client.retrieve(
            collection_name=self._collection,
            ids=[str(memory_id)],
            with_payload=True,
            with_vectors=False,
        )
        if not results:
            raise MemoryNotFoundError(str(memory_id))
        return _point_to_record(results[0])

    async def update(
        self,
        memory_id: UUID,
        update: MemoryUpdate,
        new_vector: Optional[list[float]] = None,
    ) -> MemoryRecord:
        # Verify exists
        await self.get(memory_id)
        patch: dict = {}
        if update.memory_type is not None:
            patch["memory_type"] = update.memory_type.value
        if update.category is not None:
            patch["category"] = update.category
        if update.importance_score is not None:
            patch["importance_score"] = update.importance_score
        if update.source is not None:
            patch["source"] = update.source
        if update.tags is not None:
            patch["tags"] = update.tags
        if update.session_id is not None:
            patch["session_id"] = update.session_id
        if update.content is not None:
            patch["content"] = update.content
        if update.decay_rate is not None:
            patch["decay_rate"] = update.decay_rate
        if update.pinned is not None:
            patch["pinned"] = update.pinned
        if update.project is not None:
            patch["project"] = update.project
        if update.expires_at is not None:
            patch["expires_at"] = update.expires_at.isoformat()
        if update.topic_path is not None:
            patch["topic_path"] = update.topic_path
        if update.scope is not None:
            patch["scope"] = update.scope
        if update.canonical_id is not None:
            patch["canonical_id"] = update.canonical_id

        if patch:
            await self._client.set_payload(
                collection_name=self._collection,
                payload=patch,
                points=[str(memory_id)],
            )

        if new_vector is not None:
            _validate_vector(new_vector)
            await self._client.update_vectors(
                collection_name=self._collection,
                points=[qmodels.PointVectors(id=str(memory_id), vector=new_vector)],
            )

        return await self.get(memory_id)

    async def delete(self, memory_id: UUID) -> None:
        await self.get(memory_id)  # raises 404 if not found
        await self._client.delete(
            collection_name=self._collection,
            points_selector=qmodels.PointIdsList(points=[str(memory_id)]),
        )

    async def increment_access_count(self, memory_id: UUID) -> None:
        """Peek event: increment counter only. Does NOT update last_access_ts (use-only field)."""
        record = await self.get(memory_id)
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"access_count": record.access_count + 1},
            points=[str(memory_id)],
        )

    async def apply_outcome_feedback(
        self,
        memory_ids: list[UUID],
        *,
        success: bool,
        boost: float | None = None,
        penalty: float | None = None,
    ) -> dict:
        """
        Apply hindsight outcome feedback to importance_score of the given memories.

        Success:  importance += boost   * (1 - importance)  (asymptotic to 1)
        Failure:  importance -= penalty * importance        (asymptotic to 0)

        Best-effort per-id updates (Qdrant has no native per-point arithmetic update).
        """
        if not memory_ids:
            return {"updated": 0, "skipped": 0}

        if boost is None:
            boost = float(os.getenv("OUTCOME_BOOST", "0.05"))
        if penalty is None:
            penalty = float(os.getenv("OUTCOME_PENALTY", "0.03"))

        now = datetime.now(timezone.utc).isoformat()

        results = await self._client.retrieve(
            collection_name=self._collection,
            ids=[str(x) for x in memory_ids],
            with_payload=True,
            with_vectors=False,
        )

        updated = 0
        skipped = 0
        for p in results or []:
            pl = p.payload or {}
            try:
                old = float(pl.get("importance_score", 0.5))
            except Exception:
                old = 0.5
            old = max(0.0, min(1.0, old))
            if success:
                new = old + boost * (1.0 - old)
                succ = int(pl.get("outcome_successes", 0) or 0) + 1
                fail = int(pl.get("outcome_failures", 0) or 0)
            else:
                new = old - penalty * old
                succ = int(pl.get("outcome_successes", 0) or 0)
                fail = int(pl.get("outcome_failures", 0) or 0) + 1
            new = max(0.0, min(1.0, float(new)))

            # Skip tiny deltas to avoid thrashing
            if abs(new - old) < 1e-6:
                skipped += 1
                continue

            await self._client.set_payload(
                collection_name=self._collection,
                payload={
                    "importance_score": new,
                    "last_outcome_ts": now,
                    "outcome_successes": succ,
                    "outcome_failures": fail,
                },
                points=[str(p.id)],
            )
            updated += 1

        return {"updated": updated, "skipped": skipped}

    async def mark_used(self, memory_ids: list[UUID], project: Optional[str] = None) -> None:
        """Use event: update last_access_ts for retrieved memories (called from /context)."""
        if not memory_ids:
            return
        now = datetime.now(timezone.utc)
        for mid in memory_ids:
            await self._client.set_payload(
                collection_name=self._collection,
                payload={"last_access_ts": now.isoformat()},
                points=[str(mid)],
            )
        # Update project activity tracker (module-level dict, survives within process lifetime)
        if project:
            _update_project_activity(project, now.timestamp())

    async def search(
        self,
        vector: list[float],
        agent_id: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
        category: Optional[str] = None,
        limit: int = 10,
        overfetch_factor: int = 2,
        since_minutes: Optional[int] = None,
        scope_filter: Optional[list[str]] = None,
    ) -> list[tuple[MemoryRecord, float]]:
        from datetime import timedelta
        must_conditions = []
        if agent_id:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="agent_id", match=qmodels.MatchValue(value=agent_id)
                )
            )
        if memory_type:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="memory_type", match=qmodels.MatchValue(value=memory_type.value)
                )
            )
        if category:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="category", match=qmodels.MatchValue(value=category)
                )
            )
        if since_minutes:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
            must_conditions.append(
                qmodels.FieldCondition(
                    key="timestamp", range=qmodels.DatetimeRange(gte=cutoff)
                )
            )
        if scope_filter:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="scope",
                    match=qmodels.MatchAny(any=scope_filter),
                )
            )

        query_filter = qmodels.Filter(must=must_conditions) if must_conditions else None

        _validate_vector(vector)
        results = await self._client.search(
            collection_name=self._collection,
            query_vector=vector,
            query_filter=query_filter,
            limit=limit * overfetch_factor,
            with_payload=True,
        )
        return [(_point_to_record(r), r.score) for r in results]

    async def scroll_by_topic_path(
        self,
        topic_prefix: str,
        scopes: Optional[list[str]] = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        """
        Return memories whose topic_path starts with topic_prefix.
        Optionally filtered by scope (e.g. ['domain','principle'] for canonicals).
        Uses scroll + Python-side prefix check (Qdrant has no native startswith filter).
        """
        must_conditions = []
        if scopes:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="scope",
                    match=qmodels.MatchAny(any=scopes),
                )
            )
        scroll_filter = qmodels.Filter(must=must_conditions) if must_conditions else None

        all_records: list[MemoryRecord] = []
        offset = None
        while len(all_records) < limit:
            results, next_offset = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=scroll_filter,
                limit=200,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for r in results:
                pl = r.payload or {}
                tp = pl.get("topic_path", "") or ""
                if tp == topic_prefix or tp.startswith(topic_prefix + "/"):
                    all_records.append(_point_to_record(r))
                    if len(all_records) >= limit:
                        break
            if next_offset is None:
                break
            offset = next_offset

        return all_records

    async def delete_by_filter(
        self,
        agent_id: Optional[str],
        min_importance: float,
        max_age_days: int,
    ) -> int:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()

        must_conditions: list = [
            qmodels.FieldCondition(
                key="importance_score",
                range=qmodels.Range(lt=min_importance),
            ),
            qmodels.FieldCondition(
                key="timestamp",
                range=qmodels.DatetimeRange(lt=cutoff),
            ),
        ]
        if agent_id:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="agent_id", match=qmodels.MatchValue(value=agent_id)
                )
            )

        # Count before delete
        count_before = await self._client.count(collection_name=self._collection)
        await self._client.delete(
            collection_name=self._collection,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(must=must_conditions)
            ),
        )
        count_after = await self._client.count(collection_name=self._collection)
        return count_before.count - count_after.count

    async def insert_improvement(self, improvement, vector: list[float]) -> UUID:
        _validate_vector(vector)
        memory_id = uuid4()
        now = datetime.now(timezone.utc)
        payload = {
            "content": f"{improvement.title}\n\n{improvement.description}",
            "title": improvement.title,
            "description": improvement.description,
            "project": improvement.project,
            "agent_id": improvement.agent_id,
            "memory_type": "task",
            "category": "improvement",
            "importance_score": improvement.importance_score,
            "timestamp": now.isoformat(),
            "source": "improvement",
            "tags": improvement.tags,
            "access_count": 0,
            "session_id": None,
            "status": "open",
            "resolved_at": None,
        }
        await self._client.upsert(
            collection_name=self._collection,
            points=[qmodels.PointStruct(id=str(memory_id), vector=vector, payload=payload)],
        )
        return memory_id

    async def get_improvements(
        self,
        project: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list:
        must_conditions: list = [
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="improvement"))
        ]
        if project:
            must_conditions.append(
                qmodels.FieldCondition(key="project", match=qmodels.MatchValue(value=project))
            )
        if status:
            must_conditions.append(
                qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value=status))
            )
        results, _ = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=qmodels.Filter(must=must_conditions),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [_point_to_improvement(r) for r in results]

    async def resolve_improvement(self, memory_id: UUID) -> Optional[str]:
        now = datetime.now(timezone.utc)
        results = await self._client.retrieve(
            collection_name=self._collection,
            ids=[str(memory_id)],
            with_payload=True,
        )
        if not results:
            return None

        payload = results[0].payload or {}
        project = payload.get("project") or "supermemory"
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"status": "resolved", "resolved_at": now.isoformat()},
            points=[str(memory_id)],
        )
        return project

    async def get_recent(
        self,
        minutes: int = 10,
        agent_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        must_conditions: list = [
            qmodels.FieldCondition(
                key="timestamp", range=qmodels.DatetimeRange(gte=cutoff)
            )
        ]
        if agent_id:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="agent_id", match=qmodels.MatchValue(value=agent_id)
                )
            )
        results, _ = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=qmodels.Filter(must=must_conditions),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        records = [_point_to_record(r) for r in results]
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records

    async def mark_handoff_pending(self, memory_id) -> None:
        """Set status=pending on a newly created handoff."""
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"status": "pending"},
            points=[str(memory_id)],
        )

    async def get_pending_handoffs(self, to_agent: str, limit: int = 10) -> list[dict]:
        """Return pending handoffs addressed to to_agent (category=handoff, status=pending)."""
        must_conditions: list = [
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="handoff")),
            qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value="pending")),
            qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"to:{to_agent}")),
        ]
        results, _ = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=qmodels.Filter(must=must_conditions),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        handoffs = []
        for r in results:
            tags = r.payload.get("tags", [])
            handoffs.append({
                "memory_id": str(r.id),
                "content": r.payload.get("content", ""),
                "timestamp": r.payload.get("timestamp", ""),
                "from_agent": next((t[len("from:"):] for t in tags if t.startswith("from:")), "unknown"),
                "to_agent": to_agent,
                "task_id": r.payload.get("session_id", ""),
                "tags": tags,
            })
        return handoffs

    async def mark_handoff_picked_up(self, memory_id: str) -> None:
        """Update handoff status to picked_up to prevent double-pickup."""
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"status": "picked_up"},
            points=[memory_id],
        )

    async def add_link(self, memory_id: UUID, target_id: UUID) -> None:
        """Add target_id to memory's related_ids (idempotent)."""
        record = await self.get(memory_id)
        ids = list(record.related_ids)
        target_str = str(target_id)
        if target_str not in ids:
            ids.append(target_str)
            await self._client.set_payload(
                collection_name=self._collection,
                payload={"related_ids": ids},
                points=[str(memory_id)],
            )

    async def remove_link(self, memory_id: UUID, target_id: UUID) -> None:
        """Remove target_id from memory's related_ids."""
        record = await self.get(memory_id)
        ids = [i for i in record.related_ids if i != str(target_id)]
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"related_ids": ids},
            points=[str(memory_id)],
        )

    async def get_neighbors(self, memory_id: UUID) -> list[MemoryRecord]:
        """Return all memories referenced in related_ids."""
        record = await self.get(memory_id)
        if not record.related_ids:
            return []
        results = await self._client.retrieve(
            collection_name=self._collection,
            ids=record.related_ids,
            with_payload=True,
            with_vectors=False,
        )
        return [_point_to_record(r) for r in results]

    async def collection_stats(self) -> dict:
        info = await self._client.get_collection(collection_name=self._collection)
        return {
            "points_count": info.points_count,
            "vectors_count": info.vectors_count,
            "indexed_vectors_count": info.indexed_vectors_count,
            "status": info.status.value if info.status else "unknown",
        }

    async def health(self) -> bool:
        try:
            await self._client.get_collections()
            return True
        except Exception:
            return False


def _point_to_improvement(point):
    from app.models.memory import ImprovementRecord
    p = point.payload
    resolved_at = p.get("resolved_at")
    return ImprovementRecord(
        id=UUID(str(point.id)),
        title=p.get("title", p.get("content", "")[:80]),
        description=p.get("description", p.get("content", "")),
        project=p.get("project", "supermemory"),
        agent_id=p.get("agent_id", "unknown"),
        importance_score=p.get("importance_score", 0.5),
        timestamp=datetime.fromisoformat(p["timestamp"]),
        status=p.get("status", "open"),
        tags=p.get("tags", []),
        resolved_at=datetime.fromisoformat(resolved_at) if resolved_at else None,
    )


def _point_to_record(point) -> MemoryRecord:
    p = point.payload
    last_access_raw = p.get("last_access_ts")
    return MemoryRecord(
        id=UUID(str(point.id)),
        content=p["content"],
        agent_id=p["agent_id"],
        memory_type=MemoryType(p["memory_type"]),
        category=p.get("category", "general"),
        importance_score=p.get("importance_score", 0.5),
        timestamp=datetime.fromisoformat(p["timestamp"]),
        source=p.get("source", "conversation"),
        tags=p.get("tags", []),
        access_count=p.get("access_count", 0),
        session_id=p.get("session_id"),
        status=p.get("status"),
        decay_rate=p.get("decay_rate", 1.0),
        pinned=bool(p.get("pinned", False)),
        last_access_ts=datetime.fromisoformat(last_access_raw) if last_access_raw else None,
        last_decay_ts=datetime.fromisoformat(p["last_decay_ts"]) if p.get("last_decay_ts") else None,
        related_ids=p.get("related_ids", []),
        project=p.get("project"),
        expires_at=datetime.fromisoformat(p["expires_at"]) if p.get("expires_at") else None,
        topic_path=p.get("topic_path"),
        scope=p.get("scope", "project"),
        supports=p.get("supports", []),
        canonical_id=p.get("canonical_id"),
    )
