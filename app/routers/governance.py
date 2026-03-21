"""
Memory Governance & Observability — lifecycle visibility and admin controls.

Inspired by Mem0/OpenMemory product maturity patterns.

GET  /governance/stats           — memory counts by type, category, source, age
GET  /governance/lifecycle/{id}  — full lifecycle record for a memory
GET  /governance/stale           — list stale memories (old + low access + low score)
POST /governance/bulk-tag        — add tags to many memories at once
POST /governance/bulk-decay      — update decay_rate for memories matching a filter
DELETE /governance/stale/purge   — delete stale memories matching criteria
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import QdrantDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/governance", tags=["governance"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class MemoryStats(BaseModel):
    total: int
    by_type: dict[str, int]
    by_category: dict[str, int]
    by_source: dict[str, int]
    by_status: dict[str, int]
    avg_importance: float
    avg_access_count: float
    oldest_days: Optional[float]
    newest_days: Optional[float]


class LifecycleRecord(BaseModel):
    id: str
    content_preview: str         # first 200 chars
    memory_type: str
    category: str
    importance_score: float
    decay_rate: float
    source: str
    agent_id: str
    tags: list[str]
    created_at: str              # ISO timestamp
    status: Optional[str]
    access_count: int
    age_days: float
    current_recency_score: float  # 1/(days*decay_rate+1)
    session_id: Optional[str]


class StaleMemory(BaseModel):
    id: str
    content_preview: str
    age_days: float
    access_count: int
    importance_score: float
    source: str
    reason: str  # why it's considered stale


class BulkTagRequest(BaseModel):
    agent_id: Optional[str] = None
    category: Optional[str] = None
    source_prefix: Optional[str] = Field(None, description="Match memories by source prefix")
    add_tags: list[str] = Field(..., min_length=1, max_length=10)


class BulkTagResponse(BaseModel):
    updated: int


class BulkDecayRequest(BaseModel):
    agent_id: Optional[str] = None
    category: Optional[str] = None
    memory_type: Optional[str] = None
    decay_rate: float = Field(..., ge=0.0, le=10.0, description="New decay_rate to apply")


class BulkDecayResponse(BaseModel):
    updated: int


class StaleConfig(BaseModel):
    min_age_days: int = Field(14, ge=1, description="Minimum age in days to consider stale")
    max_access_count: int = Field(0, ge=0, description="Max access count (≤ this = stale)")
    max_importance: float = Field(0.4, ge=0.0, le=1.0, description="Max importance score")
    agent_id: Optional[str] = None
    limit: int = Field(50, ge=1, le=200)


class DecayConfig(BaseModel):
    idle_days: float = Field(7.0, ge=1.0, description="Days without access before decay kicks in")
    decay_step: float = Field(0.05, ge=0.001, le=0.5, description="Importance reduction per run")
    floor: float = Field(0.05, ge=0.0, le=0.5, description="Never decay below this importance")
    agent_id: Optional[str] = None
    dry_run: bool = Field(False, description="If true, preview without applying")


class DecayCandidate(BaseModel):
    id: str
    content_preview: str
    importance_before: float
    importance_after: float
    last_access_days: float
    pinned: bool


class DecayResult(BaseModel):
    affected: int
    skipped_pinned: int
    skipped_floor: int
    skipped_recent: int
    dry_run: bool
    candidates: list[DecayCandidate]  # populated in dry_run or first 20 in apply


# ── Helpers ────────────────────────────────────────────────────────────────────

def _recency(days_old: float, decay_rate: float) -> float:
    return 1.0 / (days_old * decay_rate + 1.0)


def _age_days(timestamp_str: str) -> float:
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return 0.0


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=MemoryStats)
async def memory_stats(
    qdrant: QdrantDep,
    agent_id: Optional[str] = Query(None),
):
    """Aggregate statistics over all memories — counts, averages, distributions."""
    from qdrant_client.http import models as qmodels

    must = []
    if agent_id:
        must.append(qmodels.FieldCondition(
            key="agent_id", match=qmodels.MatchValue(value=agent_id)
        ))

    scroll_filter = qmodels.Filter(must=must) if must else None

    all_points = []
    offset = None
    while True:
        results, next_offset = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=scroll_filter,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(results)
        if next_offset is None:
            break
        offset = next_offset

    if not all_points:
        return MemoryStats(
            total=0, by_type={}, by_category={}, by_source={}, by_status={},
            avg_importance=0.0, avg_access_count=0.0,
            oldest_days=None, newest_days=None,
        )

    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total_importance = 0.0
    total_access = 0
    ages: list[float] = []

    for p in all_points:
        pl = p.payload
        by_type[pl.get("memory_type", "unknown")] = by_type.get(pl.get("memory_type", "unknown"), 0) + 1
        by_category[pl.get("category", "general")] = by_category.get(pl.get("category", "general"), 0) + 1

        src = pl.get("source", "unknown")
        # Group sources by prefix for readability
        src_key = src.split(":")[0] if ":" in src else src
        by_source[src_key] = by_source.get(src_key, 0) + 1

        status = pl.get("status") or "active"
        by_status[status] = by_status.get(status, 0) + 1

        total_importance += pl.get("importance_score", 0.5)
        total_access += pl.get("access_count", 0)

        ts = pl.get("timestamp")
        if ts:
            ages.append(_age_days(ts))

    n = len(all_points)
    return MemoryStats(
        total=n,
        by_type=dict(sorted(by_type.items(), key=lambda x: -x[1])),
        by_category=dict(sorted(by_category.items(), key=lambda x: -x[1])),
        by_source=dict(sorted(by_source.items(), key=lambda x: -x[1])),
        by_status=dict(sorted(by_status.items(), key=lambda x: -x[1])),
        avg_importance=round(total_importance / n, 3),
        avg_access_count=round(total_access / n, 3),
        oldest_days=round(max(ages), 1) if ages else None,
        newest_days=round(min(ages), 1) if ages else None,
    )


@router.get("/lifecycle/{memory_id}", response_model=LifecycleRecord)
async def memory_lifecycle(memory_id: UUID, qdrant: QdrantDep):
    """
    Full lifecycle view for a single memory — shows creation context, current state,
    recency score, and usage statistics. Useful for debugging why a memory was
    created or understanding its current relevance.
    """
    record = await qdrant.get(memory_id)
    age = _age_days(record.timestamp.isoformat())
    recency = _recency(age, record.decay_rate)

    return LifecycleRecord(
        id=str(record.id),
        content_preview=record.content[:200],
        memory_type=record.memory_type.value,
        category=record.category,
        importance_score=record.importance_score,
        decay_rate=record.decay_rate,
        source=record.source,
        agent_id=record.agent_id,
        tags=record.tags,
        created_at=record.timestamp.isoformat(),
        status=record.status,
        access_count=record.access_count,
        age_days=round(age, 2),
        current_recency_score=round(recency, 4),
        session_id=record.session_id,
    )


@router.post("/stale", response_model=list[StaleMemory])
async def list_stale(body: StaleConfig, qdrant: QdrantDep):
    """
    Find memories that are candidates for cleanup or decay adjustment.

    A memory is stale if it is old, rarely accessed, and has low importance.
    Returns a list for review before deletion — use /stale/purge to actually delete.
    """
    from qdrant_client.http import models as qmodels

    cutoff = (datetime.now(timezone.utc) - timedelta(days=body.min_age_days)).isoformat()

    must = [
        qmodels.FieldCondition(
            key="timestamp", range=qmodels.DatetimeRange(lt=cutoff)
        ),
        qmodels.FieldCondition(
            key="importance_score", range=qmodels.Range(lte=body.max_importance)
        ),
    ]
    if body.agent_id:
        must.append(qmodels.FieldCondition(
            key="agent_id", match=qmodels.MatchValue(value=body.agent_id)
        ))

    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=body.limit,
        with_payload=True,
        with_vectors=False,
    )

    stale = []
    for p in results:
        pl = p.payload
        access = pl.get("access_count", 0)
        if access > body.max_access_count:
            continue

        age = _age_days(pl.get("timestamp", ""))
        reasons = []
        if age >= body.min_age_days:
            reasons.append(f"{int(age)}d old")
        if access <= body.max_access_count:
            reasons.append(f"accessed {access}x")
        if pl.get("importance_score", 0.5) <= body.max_importance:
            reasons.append(f"importance={pl.get('importance_score', 0.5):.2f}")

        stale.append(StaleMemory(
            id=str(p.id),
            content_preview=pl.get("content", "")[:200],
            age_days=round(age, 1),
            access_count=access,
            importance_score=pl.get("importance_score", 0.5),
            source=pl.get("source", "unknown"),
            reason=", ".join(reasons),
        ))

    stale.sort(key=lambda x: x.age_days, reverse=True)
    return stale


@router.post("/bulk-tag", response_model=BulkTagResponse)
async def bulk_tag(body: BulkTagRequest, qdrant: QdrantDep):
    """Add tags to all memories matching agent_id / category / source_prefix filter."""
    from qdrant_client.http import models as qmodels

    must = []
    if body.agent_id:
        must.append(qmodels.FieldCondition(
            key="agent_id", match=qmodels.MatchValue(value=body.agent_id)
        ))
    if body.category:
        must.append(qmodels.FieldCondition(
            key="category", match=qmodels.MatchValue(value=body.category)
        ))

    if not must:
        raise HTTPException(status_code=400, detail="At least one filter (agent_id or category) is required")

    scroll_filter = qmodels.Filter(must=must)
    updated = 0
    offset = None

    while True:
        results, next_offset = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=scroll_filter,
            limit=200,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        for p in results:
            source = p.payload.get("source", "")
            if body.source_prefix and not source.startswith(body.source_prefix):
                continue

            existing_tags = p.payload.get("tags", [])
            new_tags = list(set(existing_tags) | set(body.add_tags))
            if new_tags != existing_tags:
                await qdrant._client.set_payload(
                    collection_name=qdrant._collection,
                    payload={"tags": new_tags},
                    points=[str(p.id)],
                )
                updated += 1

        if next_offset is None:
            break
        offset = next_offset

    return BulkTagResponse(updated=updated)


@router.post("/bulk-decay", response_model=BulkDecayResponse)
async def bulk_decay(body: BulkDecayRequest, qdrant: QdrantDep):
    """
    Set decay_rate for all memories matching the filter.

    Use cases:
      - decay_rate=0.0 for permanent reference memories
      - decay_rate=3.0 for fast-expiring status/news memories
      - decay_rate=1.0 to reset to default
    """
    from qdrant_client.http import models as qmodels

    must = []
    if body.agent_id:
        must.append(qmodels.FieldCondition(
            key="agent_id", match=qmodels.MatchValue(value=body.agent_id)
        ))
    if body.category:
        must.append(qmodels.FieldCondition(
            key="category", match=qmodels.MatchValue(value=body.category)
        ))
    if body.memory_type:
        must.append(qmodels.FieldCondition(
            key="memory_type", match=qmodels.MatchValue(value=body.memory_type)
        ))

    if not must:
        raise HTTPException(status_code=400, detail="At least one filter is required")

    scroll_filter = qmodels.Filter(must=must)
    updated = 0
    offset = None

    while True:
        results, next_offset = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=scroll_filter,
            limit=200,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )

        ids = [str(p.id) for p in results]
        if ids:
            await qdrant._client.set_payload(
                collection_name=qdrant._collection,
                payload={"decay_rate": body.decay_rate},
                points=ids,
            )
            updated += len(ids)

        if next_offset is None:
            break
        offset = next_offset

    return BulkDecayResponse(updated=updated)


@router.delete("/stale/purge")
async def purge_stale(
    qdrant: QdrantDep,
    min_age_days: int = Query(30, ge=1),
    max_importance: float = Query(0.3, ge=0.0, le=1.0),
    max_access_count: int = Query(0, ge=0),
    agent_id: Optional[str] = Query(None),
):
    """
    Delete stale memories matching the criteria.

    Caution: this is destructive. Use GET /governance/stale first to preview what will be deleted.
    Criteria: older than min_age_days AND importance ≤ max_importance AND access_count ≤ max_access_count.
    """
    from qdrant_client.http import models as qmodels

    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    must = [
        qmodels.FieldCondition(
            key="timestamp", range=qmodels.DatetimeRange(lt=cutoff)
        ),
        qmodels.FieldCondition(
            key="importance_score", range=qmodels.Range(lte=max_importance)
        ),
    ]
    if agent_id:
        must.append(qmodels.FieldCondition(
            key="agent_id", match=qmodels.MatchValue(value=agent_id)
        ))

    # Collect IDs to delete that also match access_count filter
    offset = None
    to_delete = []
    while True:
        results, next_offset = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qmodels.Filter(must=must),
            limit=200,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in results:
            if p.payload.get("access_count", 0) <= max_access_count:
                to_delete.append(str(p.id))

        if next_offset is None:
            break
        offset = next_offset

    if to_delete:
        await qdrant._client.delete(
            collection_name=qdrant._collection,
            points_selector=qmodels.PointIdsList(points=to_delete),
        )

    return {"deleted": len(to_delete)}


# ── Importance decay ────────────────────────────────────────────────────────────

# Categories that should never be touched by the decay job
_DECAY_SKIP_CATEGORIES = {"improvement", "skill", "handoff", "event"}


async def run_decay(qdrant, cfg: DecayConfig) -> DecayResult:
    """
    Core decay logic with project activity gate and anti-retro-decay.

    Key invariant: decay accumulates only during ACTIVE project periods.
    A sleeping project (no use-events in idle_days) is frozen — its memories
    are skipped entirely and last_decay_ts is NOT advanced.

    Anti-retro-decay formula:
        effective_now = min(now, project_last_active + grace_window)
        delta_days    = (effective_now - last_decay_ts) / 86400
        new_importance = max(floor, importance - step * decay_rate * delta_days)
        last_decay_ts  = effective_now   ← not now!
    """
    from qdrant_client.http import models as qmodels
    from app.services.qdrant_service import get_project_last_active

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    # Grace window: project is considered active up to idle_days after last use
    grace_seconds = cfg.idle_days * 86400

    must: list = []
    if cfg.agent_id:
        must.append(qmodels.FieldCondition(
            key="agent_id", match=qmodels.MatchValue(value=cfg.agent_id)
        ))
    scroll_filter = qmodels.Filter(must=must) if must else None

    all_points = []
    offset = None
    while True:
        results, next_offset = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=scroll_filter,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(results)
        if next_offset is None:
            break
        offset = next_offset

    affected = 0
    skipped_pinned = 0
    skipped_floor = 0
    skipped_recent = 0  # frozen (project sleeping)
    candidates: list[DecayCandidate] = []

    for p in all_points:
        pl = p.payload or {}

        if pl.get("category", "general") in _DECAY_SKIP_CATEGORIES:
            continue

        if pl.get("pinned", False):
            skipped_pinned += 1
            continue

        importance = float(pl.get("importance_score", 0.5))
        decay_rate = float(pl.get("decay_rate", 1.0))

        if importance <= cfg.floor:
            skipped_floor += 1
            continue

        # ── Project activity gate ──────────────────────────────────────────────
        project = pl.get("project")
        if project:
            last_active = get_project_last_active(project)
            if last_active is None or (now_ts - last_active) > grace_seconds:
                # Project is sleeping — freeze, do not decay
                skipped_recent += 1
                continue
            # effective_now = capped at last_active + grace to prevent retro-decay
            effective_ts = min(now_ts, last_active + grace_seconds)
        else:
            # No project tag: use last_access_ts as proxy (use-event timestamp)
            last_use_raw = pl.get("last_access_ts")
            if last_use_raw:
                last_use = datetime.fromisoformat(last_use_raw)
                if last_use.tzinfo is None:
                    last_use = last_use.replace(tzinfo=timezone.utc)
                last_use_ts = last_use.timestamp()
                if (now_ts - last_use_ts) > grace_seconds:
                    skipped_recent += 1
                    continue
                effective_ts = min(now_ts, last_use_ts + grace_seconds)
            else:
                # Never used — use creation timestamp, apply normal decay
                created_raw = pl.get("timestamp", "")
                created = datetime.fromisoformat(created_raw) if created_raw else now
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                effective_ts = now_ts
                # If created recently (< idle_days), skip
                if (now_ts - created.timestamp()) < grace_seconds:
                    skipped_recent += 1
                    continue

        # ── Anti-retro-decay: compute delta from last_decay_ts ─────────────────
        last_decay_raw = pl.get("last_decay_ts")
        if last_decay_raw:
            last_decay = datetime.fromisoformat(last_decay_raw)
            if last_decay.tzinfo is None:
                last_decay = last_decay.replace(tzinfo=timezone.utc)
            last_decay_ts = last_decay.timestamp()
        else:
            # First decay run — use creation time as baseline
            created_raw = pl.get("timestamp", "")
            created = datetime.fromisoformat(created_raw) if created_raw else now
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            last_decay_ts = created.timestamp()

        delta_days = max(0.0, (effective_ts - last_decay_ts) / 86400)
        if delta_days < 0.5:  # less than 12h — skip, not worth touching
            skipped_recent += 1
            continue

        new_importance = max(cfg.floor, importance - cfg.decay_step * decay_rate * delta_days)
        new_last_decay = datetime.fromtimestamp(effective_ts, tz=timezone.utc).isoformat()

        last_access_days = (now_ts - (last_decay_ts)) / 86400

        candidate = DecayCandidate(
            id=str(p.id),
            content_preview=pl.get("content", "")[:120],
            importance_before=round(importance, 4),
            importance_after=round(new_importance, 4),
            last_access_days=round(last_access_days, 1),
            pinned=bool(pl.get("pinned", False)),
        )
        candidates.append(candidate)
        affected += 1

        if not cfg.dry_run:
            await qdrant._client.set_payload(
                collection_name=qdrant._collection,
                payload={
                    "importance_score": new_importance,
                    "last_decay_ts": new_last_decay,
                },
                points=[str(p.id)],
            )

    return DecayResult(
        affected=affected,
        skipped_pinned=skipped_pinned,
        skipped_floor=skipped_floor,
        skipped_recent=skipped_recent,
        dry_run=cfg.dry_run,
        candidates=candidates if cfg.dry_run else candidates[:20],
    )


@router.post("/decay", response_model=DecayResult)
async def decay_importance(body: DecayConfig, qdrant: QdrantDep):
    """
    Apply (or preview) importance decay to inactive memories.

    Decays importance_score of memories not accessed within idle_days.
    Skips: pinned memories, skills, improvements, handoffs, memories already at floor.
    Set dry_run=true to preview without making changes.
    """
    return await run_decay(qdrant, body)


# ── Spaced resurfacing (dying memories) ────────────────────────────────────────

class DyingMemory(BaseModel):
    id: str
    content_preview: str
    importance_score: float
    last_access_days: float
    age_days: float
    access_count: int
    tags: list[str]
    decay_rate: float
    pinned: bool


@router.get("/dying", response_model=list[DyingMemory])
async def dying_memories(
    qdrant: QdrantDep,
    limit: int = Query(10, ge=1, le=50),
    max_importance: float = Query(0.25, ge=0.0, le=1.0, description="Only surface memories below this importance"),
    min_age_days: float = Query(14.0, ge=1.0, description="Only memories older than this"),
    min_idle_days: float = Query(7.0, ge=1.0, description="Not accessed within this many days"),
    agent_id: Optional[str] = Query(None),
):
    """
    Return memories approaching irrelevance — low importance, old, and not recently accessed.

    These are candidates for: Pin (save from decay), Boost importance, or Delete.
    Sorted by importance ascending (most at-risk first).
    """
    from qdrant_client.http import models as qmodels

    now = datetime.now(timezone.utc)
    age_cutoff = (now - timedelta(days=min_age_days)).isoformat()

    must: list = [
        qmodels.FieldCondition(
            key="importance_score", range=qmodels.Range(lte=max_importance)
        ),
        qmodels.FieldCondition(
            key="timestamp", range=qmodels.DatetimeRange(lt=age_cutoff)
        ),
    ]
    if agent_id:
        must.append(qmodels.FieldCondition(
            key="agent_id", match=qmodels.MatchValue(value=agent_id)
        ))

    results, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=limit * 5,  # oversample to allow idle_days filter in Python
        with_payload=True,
        with_vectors=False,
    )

    idle_cutoff_dt = now - timedelta(days=min_idle_days)
    dying: list[DyingMemory] = []

    for p in results:
        pl = p.payload or {}
        category = pl.get("category", "general")
        if category in _DECAY_SKIP_CATEGORIES:
            continue

        last_ts_raw = pl.get("last_access_ts") or pl.get("timestamp", "")
        last_ts = datetime.fromisoformat(last_ts_raw) if last_ts_raw else now
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        if last_ts > idle_cutoff_dt:
            continue  # accessed recently — not dying

        created_ts_raw = pl.get("timestamp", "")
        age = _age_days(created_ts_raw)
        last_access_days = (now - last_ts).total_seconds() / 86400

        dying.append(DyingMemory(
            id=str(p.id),
            content_preview=pl.get("content", "")[:150],
            importance_score=float(pl.get("importance_score", 0.5)),
            last_access_days=round(last_access_days, 1),
            age_days=round(age, 1),
            access_count=int(pl.get("access_count", 0)),
            tags=pl.get("tags", []),
            decay_rate=float(pl.get("decay_rate", 1.0)),
            pinned=bool(pl.get("pinned", False)),
        ))

    dying.sort(key=lambda m: m.importance_score)
    return dying[:limit]


@router.patch("/dying/{memory_id}/pin")
async def pin_memory(memory_id: UUID, qdrant: QdrantDep):
    """Pin a dying memory to protect it from future decay."""
    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload={"pinned": True},
        points=[str(memory_id)],
    )
    return {"id": str(memory_id), "pinned": True}


@router.patch("/dying/{memory_id}/boost")
async def boost_memory(
    memory_id: UUID,
    qdrant: QdrantDep,
    importance: float = Query(0.7, ge=0.0, le=1.0),
):
    """Boost importance_score of a dying memory."""
    await qdrant._client.set_payload(
        collection_name=qdrant._collection,
        payload={"importance_score": importance},
        points=[str(memory_id)],
    )
    return {"id": str(memory_id), "importance_score": importance}
