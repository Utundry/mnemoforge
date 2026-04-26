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

_HANDOFF_PAYLOAD_CONTENT_PREFIX = "handoff_ref:"

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


def _handoff_payload_ref(memory_id: UUID | str) -> str:
    return f"{_HANDOFF_PAYLOAD_CONTENT_PREFIX}{memory_id}"


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
        full_payload = {
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
            "status": memory.status,
            "meta": memory.meta,
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
        store_content = memory.content
        store_metadata = _metadata_without_content(full_payload)
        payload = dict(full_payload)
        if memory.category == "handoff":
            # Handoff packets keep full metadata/content in SQLite for durability.
            payload["content"] = _handoff_payload_ref(memory_id)
            payload["meta"] = {}
        await self._client.upsert(
            collection_name=self._collection,
            points=[qmodels.PointStruct(id=str(memory_id), vector=vector, payload=payload)],
        )
        await _persist_memory_to_store(
            memory_id,
            payload,
            store_content=store_content,
            store_metadata=store_metadata,
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
        record = _point_to_record(results[0])
        return await _hydrate_record(record)

    async def update(
        self,
        memory_id: UUID,
        update: MemoryUpdate,
        new_vector: Optional[list[float]] = None,
    ) -> MemoryRecord:
        # Verify exists
        existing = await self.get(memory_id)
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
        if update.status is not None:
            patch["status"] = update.status
        if update.meta is not None:
            patch["meta"] = update.meta
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

        store_patch = dict(patch)
        store_content = store_patch.pop("content", None)
        category_after = update.category if update.category is not None else existing.category
        if category_after == "handoff":
            # Keep Qdrant handoff payload lightweight; SQLite stores full packet metadata/content.
            patch["content"] = _handoff_payload_ref(memory_id)
            patch["meta"] = {}

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

        if store_content is not None or store_patch:
            await _sync_memory_store(memory_id, content=store_content, metadata_patch=store_patch)

        return await self.get(memory_id)

    async def delete(self, memory_id: UUID) -> None:
        await self.get(memory_id)  # raises 404 if not found
        await self._client.delete(
            collection_name=self._collection,
            points_selector=qmodels.PointIdsList(points=[str(memory_id)]),
        )
        await _remove_memory_from_store(memory_id)

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
        iso_now = now.isoformat()
        for mid in memory_ids:
            await self._client.set_payload(
                collection_name=self._collection,
                payload={"last_access_ts": iso_now},
                points=[str(mid)],
            )
            await _sync_memory_store(mid, metadata_patch={"last_access_ts": iso_now})
        # Update project activity tracker (module-level dict, survives within process lifetime)
        if project:
            _update_project_activity(project, now.timestamp())

    async def search(
        self,
        vector: list[float],
        agent_id: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
        category: Optional[str] = None,
        topic_prefix: Optional[str] = None,
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
        if topic_prefix:
            prefix = str(topic_prefix).strip().strip("/")
            if prefix:
                topic_ids = await self._collect_topic_prefix_ids(
                    topic_prefix=prefix,
                    must_conditions=list(must_conditions),
                    limit=max(limit * overfetch_factor * 20, 200),
                )
                if not topic_ids:
                    return []
                must_conditions.append(qmodels.HasIdCondition(has_id=topic_ids))

        query_filter = qmodels.Filter(must=must_conditions) if must_conditions else None

        _validate_vector(vector)
        results = await self._client.search(
            collection_name=self._collection,
            query_vector=vector,
            query_filter=query_filter,
            limit=limit * overfetch_factor,
            with_payload=True,
        )
        records = [_point_to_record(r) for r in results]
        records = await _hydrate_records(records)
        return [(records[i], results[i].score) for i in range(len(records))]

    async def _collect_topic_prefix_ids(
        self,
        *,
        topic_prefix: str,
        must_conditions: list,
        limit: int,
    ) -> list[str]:
        """Collect IDs whose topic_path starts with topic_prefix under existing filters."""
        scroll_filter = qmodels.Filter(must=must_conditions) if must_conditions else None
        matched_ids: list[str] = []
        offset = None

        while len(matched_ids) < limit:
            batch, next_offset = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=scroll_filter,
                limit=200,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if not batch:
                break
            for point in batch:
                payload = point.payload or {}
                tp = str(payload.get("topic_path") or "").strip()
                if tp == topic_prefix or tp.startswith(topic_prefix + "/"):
                    matched_ids.append(str(point.id))
                    if len(matched_ids) >= limit:
                        break
            if next_offset is None:
                break
            offset = next_offset
        return matched_ids

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

        return await _hydrate_records(all_records)

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

    async def resolve_improvement(
        self,
        memory_id: UUID,
        *,
        acted_by: str = "user",
        action_source: str = "inline_user_approval",
        reason: str = "",
    ) -> Optional[str]:
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
            payload={
                "status": "resolved",
                "resolved_at": now.isoformat(),
                "last_status_action": "resolve_improvement",
                "last_status_acted_by": acted_by,
                "last_status_action_source": action_source,
                "last_status_action_at": now.isoformat(),
                "last_status_action_reason": reason or None,
            },
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
        records = await _hydrate_records(records)
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records

    async def mark_handoff_pending(self, memory_id) -> None:
        """Set status=pending on a newly created handoff."""
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"status": "pending"},
            points=[str(memory_id)],
        )

    async def _load_handoff_entries_from_store(self, *, limit: int) -> list[dict]:
        from app.services.memory_store import get_memory_store

        rows = await get_memory_store().list_by_category("memory", limit=max(limit, 100))
        entries: list[dict] = []
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            if metadata.get("category") != "handoff":
                continue
            entries.append(
                {
                    "id": str(row.get("memory_id") or ""),
                    "payload": metadata,
                    "content": str(row.get("content") or ""),
                }
            )
        return entries

    async def _hydrate_handoff_entries_from_store(self, entries: list[dict]) -> list[dict]:
        if not entries:
            return entries
        from app.services.memory_store import get_memory_store

        ids = [str(item.get("id") or "").strip() for item in entries if str(item.get("id") or "").strip()]
        if not ids:
            return entries
        store = get_memory_store()
        rows = await store.get_many(ids)
        hydrated: list[dict] = []
        for item in entries:
            memory_id = str(item.get("id") or "").strip()
            payload = dict(item.get("payload") or {})
            content = str(payload.get("content") or item.get("content") or "")
            row = rows.get(memory_id)
            if row is None and memory_id:
                row = await store.get(memory_id)
            if row:
                row_meta = dict(row.get("metadata") or {})
                payload = {**payload, **row_meta}
                row_content = str(row.get("content") or "")
                if row_content:
                    content = row_content
            payload["content"] = content
            hydrated.append({"id": memory_id, "payload": payload, "content": content})
        return hydrated

    async def _list_handoffs(
        self,
        *,
        to_agent: str,
        limit: int = 10,
        handoff_label: str | None = None,
        statuses: list[str] | None = None,
        owner_agent: str | None = None,
        write_scope: list[str] | None = None,
    ) -> list[dict]:
        def _extract_line_value(content: str, prefix: str) -> str:
            needle = f"{prefix}:"
            for line in content.splitlines():
                if line.startswith(needle):
                    return line[len(needle):].strip()
            return ""

        def _extract_csv(content: str, prefix: str) -> list[str]:
            raw = _extract_line_value(content, prefix)
            return [item.strip() for item in raw.split(",") if item.strip()]

        must_conditions: list = [
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="handoff")),
            qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"to:{to_agent}")),
        ]
        requested_statuses = [str(item).strip() for item in (statuses or []) if str(item).strip()]
        if len(requested_statuses) == 1:
            must_conditions.append(qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value=requested_statuses[0])))
        elif requested_statuses:
            must_conditions.append(
                qmodels.FieldCondition(key="status", match=qmodels.MatchAny(any=requested_statuses))
            )
        if handoff_label:
            must_conditions.append(
                qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"handoff_label:{handoff_label}"))
            )
        scan_limit = max(limit, 100) if write_scope else limit
        entries: list[dict] = []
        using_fallback = False
        try:
            results, _ = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=qmodels.Filter(must=must_conditions),
                limit=scan_limit,
                with_payload=True,
                with_vectors=False,
            )
            entries = [
                {
                    "id": str(r.id),
                    "payload": dict(r.payload or {}),
                    "content": str((r.payload or {}).get("content") or ""),
                }
                for r in results
            ]
            entries = await self._hydrate_handoff_entries_from_store(entries)
        except Exception as e:
            logger.warning("handoff scroll failed, falling back to SQLite memory store: %s", e)
            using_fallback = True
            from app.services.data_integrity_service import HANDOFF_STATUS_FILTER_SLICE_ID
            from app.services.data_integrity_service import get_data_integrity_store

            entries = await self._load_handoff_entries_from_store(limit=max(scan_limit * 5, 500))
            get_data_integrity_store().upsert_slice(
                slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
                subsystem="qdrant",
                status="degraded",
                source="qdrant._list_handoffs",
                error=str(e),
                details={"fallback": "sqlite_memory_store"},
            )
        handoffs = []
        for entry in entries:
            payload = entry.get("payload") or {}
            tags = list(payload.get("tags", []) or [])
            content = str(entry.get("content") or payload.get("content") or "")
            meta = dict(payload.get("meta") or {})
            status_value = str(payload.get("status", "pending") or "pending").strip() or "pending"
            resolved_to_agent = next((t[len("to:"):] for t in tags if t.startswith("to:")), "").strip()
            if resolved_to_agent == "":
                resolved_to_agent = str(meta.get("to_agent") or _extract_line_value(content, "to_agent") or "").strip()
            if resolved_to_agent == "":
                resolved_to_agent = str(_extract_line_value(content, "to_agent") or _extract_line_value(content, "to") or "").strip()
            resolved_handoff_label = next((t[len("handoff_label:"):] for t in tags if t.startswith("handoff_label:")), "")
            if not resolved_handoff_label:
                resolved_handoff_label = str(meta.get("handoff_label") or _extract_line_value(content, "handoff_label") or "").strip()
            resolved_owner_agent = str(meta.get("owner_agent") or _extract_line_value(content, "owner_agent") or "").strip()
            if resolved_to_agent != to_agent:
                continue
            if requested_statuses and status_value not in requested_statuses:
                continue
            if handoff_label and resolved_handoff_label != handoff_label:
                continue
            if owner_agent and resolved_owner_agent != owner_agent:
                continue
            handoffs.append({
                "memory_id": str(entry.get("id") or ""),
                "status": status_value,
                "content": content,
                "timestamp": payload.get("timestamp", ""),
                "from_agent": (
                    next((t[len("from:"):] for t in tags if t.startswith("from:")), "").strip()
                    or str(meta.get("from_agent") or _extract_line_value(content, "from_agent") or "unknown").strip()
                ),
                "to_agent": resolved_to_agent,
                "task_id": payload.get("session_id", ""),
                "handoff_label": resolved_handoff_label,
                "project_id": str(meta.get("project_id") or _extract_line_value(content, "project_id") or "").strip(),
                "owner_agent": resolved_owner_agent,
                "write_scope": meta.get("write_scope") or _extract_csv(content, "write_scope"),
                "executor_used": str(meta.get("executor_used") or _extract_line_value(content, "executor_used") or "").strip(),
                "model_used": str(meta.get("model_used") or _extract_line_value(content, "model_used") or "").strip(),
                "result_summary": str(meta.get("result_summary") or _extract_line_value(content, "result_summary") or "").strip(),
                "verification_summary": str(meta.get("verification_summary") or _extract_line_value(content, "verification_summary") or "").strip(),
                "phase": str(meta.get("phase") or _extract_line_value(content, "phase") or "").strip(),
                "priority": str(meta.get("priority") or _extract_line_value(content, "priority") or "").strip(),
                "why_now": str(meta.get("why_now") or _extract_line_value(content, "why_now") or "").strip(),
                "definition_of_done": str(meta.get("definition_of_done") or _extract_line_value(content, "definition_of_done") or "").strip(),
                "expected_output_shape": str(meta.get("expected_output_shape") or _extract_line_value(content, "expected_output_shape") or "").strip(),
                "phase_objective": str(meta.get("phase_objective") or _extract_line_value(content, "phase_objective") or "").strip(),
                "execution_mode": str(meta.get("execution_mode") or _extract_line_value(content, "execution_mode") or "").strip(),
                "background_job_type": str(meta.get("background_job_type") or _extract_line_value(content, "background_job_type") or "").strip(),
                "background_job_status": str(meta.get("background_job_status") or "").strip(),
                "dispatched_job_id": str(meta.get("dispatched_job_id") or "").strip(),
                "suggested_execution_tier": str(meta.get("suggested_execution_tier") or _extract_line_value(content, "suggested_execution_tier") or "").strip(),
                "model_hint": str(meta.get("model_hint") or _extract_line_value(content, "model_hint") or "").strip(),
                "core_instinct_ids": meta.get("core_instinct_ids") or _extract_csv(content, "core_instinct_ids"),
                "supporting_instinct_ids": meta.get("supporting_instinct_ids") or _extract_csv(content, "supporting_instinct_ids"),
                "project_context_summary": (meta.get("project_context_summary") or ""),
                "project_context_refs": (meta.get("project_context_refs") or {}),
                "project_context_snapshot": (meta.get("project_context_snapshot") or ""),
                "tags": tags,
            })
        if using_fallback and not handoffs:
            logger.info("handoff fallback returned no matching packets for to_agent=%s", to_agent)
        requested_scope = [str(item).strip() for item in (write_scope or []) if str(item).strip()]
        if requested_scope:
            handoffs = [
                item
                for item in handoffs
                if all(scope in (item.get("write_scope") or []) for scope in requested_scope)
            ]
        handoffs.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return handoffs[:limit]

    async def get_pending_handoffs(self, to_agent: str, limit: int = 10, handoff_label: str | None = None) -> list[dict]:
        """Return pending handoffs addressed to to_agent (category=handoff, status=pending)."""
        return await self._list_handoffs(
            to_agent=to_agent,
            limit=limit,
            handoff_label=handoff_label,
            statuses=["pending"],
        )

    async def list_handoffs(
        self,
        *,
        to_agent: str,
        limit: int = 20,
        handoff_label: str | None = None,
        statuses: list[str] | None = None,
        owner_agent: str | None = None,
        write_scope: list[str] | None = None,
    ) -> list[dict]:
        """Return handoffs for an agent filtered by lifecycle statuses."""
        return await self._list_handoffs(
            to_agent=to_agent,
            limit=limit,
            handoff_label=handoff_label,
            statuses=statuses,
            owner_agent=owner_agent,
            write_scope=write_scope,
        )

    async def list_background_handoffs(
        self,
        *,
        limit: int = 100,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        """Return handoff packets that have background dispatch metadata."""
        must_conditions: list = [
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="handoff")),
        ]
        requested_statuses = [str(item).strip() for item in (statuses or []) if str(item).strip()]
        if len(requested_statuses) == 1:
            must_conditions.append(qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value=requested_statuses[0])))
        elif requested_statuses:
            must_conditions.append(
                qmodels.FieldCondition(key="status", match=qmodels.MatchAny(any=requested_statuses))
            )
        entries: list[dict] = []
        try:
            results, _ = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=qmodels.Filter(must=must_conditions),
                limit=max(limit, 200),
                with_payload=True,
                with_vectors=False,
            )
            entries = [
                {
                    "id": str(r.id),
                    "payload": dict(r.payload or {}),
                }
                for r in results
            ]
            entries = await self._hydrate_handoff_entries_from_store(entries)
        except Exception as e:
            logger.warning("background handoff scroll failed, falling back to SQLite memory store: %s", e)
            from app.services.data_integrity_service import HANDOFF_STATUS_FILTER_SLICE_ID
            from app.services.data_integrity_service import get_data_integrity_store

            entries = await self._load_handoff_entries_from_store(limit=max(limit * 5, 500))
            get_data_integrity_store().upsert_slice(
                slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
                subsystem="qdrant",
                status="degraded",
                source="qdrant.list_background_handoffs",
                error=str(e),
                details={"fallback": "sqlite_memory_store"},
            )
        items: list[dict] = []
        for entry in entries:
            payload = entry.get("payload") or {}
            status_value = str(payload.get("status", "pending") or "pending").strip() or "pending"
            if requested_statuses and status_value not in requested_statuses:
                continue
            meta = dict(payload.get("meta") or {})
            dispatched_job_id = str(meta.get("dispatched_job_id") or "").strip()
            background_job_type = str(meta.get("background_job_type") or "").strip()
            if not dispatched_job_id or not background_job_type:
                continue
            tags = list(payload.get("tags", []) or [])
            items.append(
                {
                    "memory_id": str(entry.get("id") or ""),
                    "status": status_value,
                    "timestamp": payload.get("timestamp", ""),
                    "task_id": payload.get("session_id", ""),
                    "handoff_label": (
                        next((t[len("handoff_label:"):] for t in tags if t.startswith("handoff_label:")), "").strip()
                        or str(meta.get("handoff_label") or "").strip()
                    ),
                    "to_agent": (
                        next((t[len("to:"):] for t in tags if t.startswith("to:")), "").strip()
                        or str(meta.get("to_agent") or "").strip()
                    ),
                    "owner_agent": str(meta.get("owner_agent") or "").strip(),
                    "background_job_type": background_job_type,
                    "background_job_status": str(meta.get("background_job_status") or "").strip(),
                    "dispatched_job_id": dispatched_job_id,
                    "executor_used": str(meta.get("executor_used") or "").strip(),
                    "model_used": str(meta.get("model_used") or "").strip(),
                }
            )
        items.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return items[:limit]

    async def list_pending_handoff_labels(self, to_agent: str, limit: int = 20, scan_limit: int = 100) -> list[dict]:
        """Return unique pending handoff labels for an agent, with light summary data."""
        must_conditions: list = [
            qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="handoff")),
            qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value="pending")),
            qmodels.FieldCondition(key="tags", match=qmodels.MatchValue(value=f"to:{to_agent}")),
        ]
        entries: list[dict] = []
        try:
            results, _ = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=qmodels.Filter(must=must_conditions),
                limit=max(limit, scan_limit),
                with_payload=True,
                with_vectors=False,
            )
            entries = [
                {
                    "id": str(r.id),
                    "payload": dict(r.payload or {}),
                }
                for r in results
            ]
            entries = await self._hydrate_handoff_entries_from_store(entries)
        except Exception as e:
            logger.warning("pending handoff labels scroll failed, falling back to SQLite memory store: %s", e)
            from app.services.data_integrity_service import HANDOFF_STATUS_FILTER_SLICE_ID
            from app.services.data_integrity_service import get_data_integrity_store

            entries = await self._load_handoff_entries_from_store(limit=max(limit, scan_limit) * 5)
            get_data_integrity_store().upsert_slice(
                slice_id=HANDOFF_STATUS_FILTER_SLICE_ID,
                subsystem="qdrant",
                status="degraded",
                source="qdrant.list_pending_handoff_labels",
                error=str(e),
                details={"fallback": "sqlite_memory_store"},
            )
        grouped: dict[str, dict] = {}
        for entry in entries:
            payload = entry.get("payload") or {}
            tags = list(payload.get("tags", []) or [])
            status_value = str(payload.get("status", "pending") or "pending").strip() or "pending"
            if status_value != "pending":
                continue
            meta = dict(payload.get("meta") or {})
            resolved_to_agent = (
                next((t[len("to:"):] for t in tags if t.startswith("to:")), "").strip()
                or str(meta.get("to_agent") or "").strip()
            )
            if resolved_to_agent != to_agent:
                continue
            label = (
                next((t[len("handoff_label:"):] for t in tags if t.startswith("handoff_label:")), "").strip()
                or str(meta.get("handoff_label") or "").strip()
            )
            if not label:
                continue
            item = grouped.setdefault(
                label,
                {
                    "handoff_label": label,
                    "count": 0,
                    "latest_timestamp": "",
                    "latest_task_id": "",
                    "from_agents": set(),
                },
            )
            item["count"] += 1
            item["from_agents"].add(
                next((t[len("from:"):] for t in tags if t.startswith("from:")), "").strip()
                or str(meta.get("from_agent") or "unknown").strip()
            )
            ts = str(payload.get("timestamp", "") or "")
            if ts >= item["latest_timestamp"]:
                item["latest_timestamp"] = ts
                item["latest_task_id"] = str(payload.get("session_id", "") or "")
        items = [
            {
                "handoff_label": label,
                "count": data["count"],
                "latest_timestamp": data["latest_timestamp"],
                "latest_task_id": data["latest_task_id"],
                "from_agents": sorted(data["from_agents"]),
            }
            for label, data in grouped.items()
        ]
        items.sort(key=lambda item: (item.get("latest_timestamp", ""), item["handoff_label"]), reverse=True)
        return items[:limit]

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
            await _sync_memory_store(memory_id, metadata_patch={"related_ids": ids})

    async def remove_link(self, memory_id: UUID, target_id: UUID) -> None:
        """Remove target_id from memory's related_ids."""
        record = await self.get(memory_id)
        ids = [i for i in record.related_ids if i != str(target_id)]
        await self._client.set_payload(
            collection_name=self._collection,
            payload={"related_ids": ids},
            points=[str(memory_id)],
        )
        await _sync_memory_store(memory_id, metadata_patch={"related_ids": ids})

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
        records = [_point_to_record(r) for r in results]
        return await _hydrate_records(records)

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
        meta=p.get("meta", {}),
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


def _metadata_without_content(payload: dict) -> dict:
    metadata = dict(payload)
    metadata.pop("content", None)
    return metadata


def _apply_store_row_to_record(record: MemoryRecord, row: dict | None) -> MemoryRecord:
    if not row:
        return record
    content = row.get("content")
    if isinstance(content, str) and content:
        record.content = content
    metadata = row.get("metadata", {}) or {}
    meta_override = metadata.get("meta")
    if isinstance(meta_override, dict):
        record.meta = meta_override
    return record


async def _hydrate_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    if not records:
        return records
    from app.services.memory_store import get_memory_store

    store = get_memory_store()
    ids = [str(record.id) for record in records]
    store_data = await store.get_many(ids)
    for record in records:
        row = store_data.get(str(record.id))
        _apply_store_row_to_record(record, row)
    return records


async def _hydrate_record(record: MemoryRecord) -> MemoryRecord:
    hydrated = await _hydrate_records([record])
    return hydrated[0]


async def _persist_memory_to_store(
    memory_id: UUID,
    payload: dict,
    *,
    store_content: str | None = None,
    store_metadata: dict | None = None,
) -> None:
    from app.services.memory_store import get_memory_store

    store = get_memory_store()
    await store.upsert(
        str(memory_id),
        "memory",
        payload.get("content", "") if store_content is None else store_content,
        _metadata_without_content(payload) if store_metadata is None else store_metadata,
    )


async def _sync_memory_store(memory_id: UUID, *, content: str | None = None, metadata_patch: dict | None = None) -> None:
    if content is None and not metadata_patch:
        return
    from app.services.memory_store import get_memory_store

    store = get_memory_store()
    mem_id = str(memory_id)
    if content is not None:
        existing = await store.get(mem_id)
        metadata = existing.get("metadata", {}) if existing else {}
        if metadata_patch:
            metadata = {**metadata, **metadata_patch}
        await store.upsert(mem_id, "memory", content, metadata)
        return
    await store.patch_metadata(mem_id, metadata_patch or {})


async def _remove_memory_from_store(memory_id: UUID) -> None:
    from app.services.memory_store import get_memory_store

    await get_memory_store().delete(str(memory_id))
