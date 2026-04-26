"""
Crystallization service for progressive consolidation of memories into canonicals.

Levels:
  L0 config
  L1 project
  L2 family
  L3 domain
  L4 principle
  L5 meta
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.governed_artifact import (
    apply_candidate_fields,
    build_candidate_revision,
    clear_prefixed_candidate_patch,
    extract_prefixed_candidate,
    prefixed_candidate_patch,
)
from app.services.data_hygiene_service import memory_payload_should_be_excluded_from_learning
from app.services.project_tree_store import get_tree_store
from app.services.qdrant_service import _point_to_record

logger = logging.getLogger(__name__)

SCOPE_ORDER = ["config", "project", "family", "domain", "principle", "meta"]
LEAF_SCOPES = {"config", "project", "family"}
CANONICAL_SCOPES = {"domain", "principle", "meta"}

MIN_SUPPORTS_FOR_DOMAIN = int(os.getenv("CRYSTAL_MIN_SUPPORTS", "3"))
MIN_PROJECTS_FOR_DOMAIN = int(os.getenv("CRYSTAL_MIN_PROJECTS", "2"))
MIN_SUPPORTS_FOR_PRINCIPLE = int(os.getenv("CRYSTAL_MIN_SUPPORTS_PRINCIPLE", "5"))
MIN_DOMAINS_FOR_PRINCIPLE = int(os.getenv("CRYSTAL_MIN_DOMAINS_PRINCIPLE", "2"))
MIN_SUPPORTS_FOR_META = int(os.getenv("CRYSTAL_MIN_SUPPORTS_META", "4"))
MIN_PRINCIPLES_FOR_META = int(os.getenv("CRYSTAL_MIN_PRINCIPLES_META", "2"))
CLUSTER_SIMILARITY_THRESHOLD = float(os.getenv("CRYSTAL_SIMILARITY", "0.72"))
CANDIDATE_CONFIDENCE_THRESHOLD = float(os.getenv("CRYSTAL_CONFIDENCE_MIN", "0.4"))
CANONICAL_MERGE_THRESHOLD = float(os.getenv("CRYSTAL_MERGE_SIMILARITY", "0.88"))
CANONICAL_SUPPRESS_THRESHOLD = float(os.getenv("CRYSTAL_SUPPRESS_CONFIDENCE", "0.45"))

_SKIP_CATEGORIES = {"improvement", "skill", "handoff", "event", "status", "incident"}
CANONICAL_CANDIDATE_FIELDS = (
    "content",
    "topic_path",
    "supports",
    "support_count",
    "confidence",
    "source_scope",
    "observation",
    "why_it_matters",
    "updated_at",
)


@dataclass
class MemoryEvidence:
    memory_id: str
    content: str
    topic_path: str
    scope: str
    project: Optional[str]
    agent_id: str
    importance_score: float
    access_count: int
    category: str
    tags: list[str]
    supports: list[str] = field(default_factory=list)


@dataclass
class Cluster:
    topic_path: str
    items: list[MemoryEvidence]
    avg_similarity: float
    source_set: set[str]
    source_scope: str
    confidence: float = 0.0
    target_scope: str = "domain"


@dataclass
class CrystallizationCandidate:
    key: str
    topic_path: str
    target_scope: str
    statement: str
    observation: str
    why_it_matters: str
    supports: list[str]
    confidence: float
    project_diversity: int
    evidence_count: int
    source_scope: str = "project"


def _candidate_key(topic_path: str, target_scope: str) -> str:
    raw = f"{topic_path}::{target_scope}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _topic_group(topic_path: str, depth: int, fallback_scope: str) -> str:
    parts = [p for p in (topic_path or "").split("/") if p]
    if not parts:
        return fallback_scope
    if depth <= 0:
        return parts[0]
    return "/".join(parts[:depth])


def _common_topic_prefix(topic_paths: list[str]) -> str:
    parts = [[segment for segment in tp.split("/") if segment] for tp in topic_paths if tp]
    if not parts:
        return ""
    prefix = parts[0]
    for candidate in parts[1:]:
        while prefix and prefix != candidate[: len(prefix)]:
            prefix = prefix[:-1]
    return "/".join(prefix)


def _derive_topic_path(topic_group: str, items: list[MemoryEvidence], target_scope: str) -> str:
    derived = topic_group.strip("/")
    if derived:
        return derived
    prefix = _common_topic_prefix([item.topic_path for item in items])
    if prefix:
        return prefix
    return f"{target_scope}/general"


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _source_identity(item: MemoryEvidence, source_scope: str) -> str:
    if source_scope in LEAF_SCOPES:
        return item.project or item.agent_id or item.topic_path or item.memory_id
    return item.topic_path or item.memory_id


def _min_supports_for_scope(scope: str) -> int:
    if scope == "domain":
        return MIN_SUPPORTS_FOR_DOMAIN
    if scope == "principle":
        return MIN_SUPPORTS_FOR_PRINCIPLE
    if scope == "meta":
        return MIN_SUPPORTS_FOR_META
    return 1


def _compute_confidence(cluster: Cluster, *, min_supports: int, min_diversity: int) -> float:
    sim = min(cluster.avg_similarity, 1.0)
    diversity = min(len(cluster.source_set) / max(min_diversity, 1), 1.0)
    support_signal = min(len(cluster.items) / max(min_supports, 1), 1.0)
    time_sensitive = sum(
        1
        for item in cluster.items
        if item.category in {"status", "incident", "price", "ops", "news"}
    )
    ts_penalty = 0.2 * (time_sensitive / max(len(cluster.items), 1))
    return round(max(0.0, sim * 0.45 + diversity * 0.25 + support_signal * 0.3 - ts_penalty), 3)


def _avg_cosine(vectors: list[list[float]]) -> float:
    if len(vectors) < 2:
        return 0.5
    import math

    total = 0.0
    count = 0
    sample = vectors[:10]
    for i in range(len(sample)):
        for j in range(i + 1, len(sample)):
            first, second = sample[i], sample[j]
            dot = sum(x * y for x, y in zip(first, second))
            norm_first = math.sqrt(sum(x * x for x in first))
            norm_second = math.sqrt(sum(x * x for x in second))
            if norm_first > 0 and norm_second > 0:
                total += dot / (norm_first * norm_second)
                count += 1
    return round(total / count, 4) if count else 0.5


def _draft_statement(cluster: Cluster) -> str:
    scope_label = {
        "family": "shared pattern",
        "domain": "domain pattern",
        "principle": "engineering principle",
        "meta": "meta heuristic",
    }.get(cluster.target_scope, "pattern")
    contexts = ", ".join(sorted(cluster.source_set)[:3])
    tail = "..." if len(cluster.source_set) > 3 else ""
    return (
        f"[{scope_label.upper()}] {cluster.topic_path}: recurring knowledge consolidated from "
        f"{len(cluster.items)} evidence records across {len(cluster.source_set)} context(s) "
        f"({contexts}{tail})."
    )


async def _refine_statement(
    cluster: Cluster,
    ollama_svc,
) -> str:
    fallback = _draft_statement(cluster)
    generate = getattr(ollama_svc, "generate", None)
    if generate is None:
        return fallback

    support_lines = "\n".join(
        f"- ({item.scope}) {item.content[:180]}"
        for item in cluster.items[:4]
        if item.content
    )
    prompt = (
        "Write one concise canonical knowledge statement.\n"
        f"Target scope: {cluster.target_scope}\n"
        f"Topic path: {cluster.topic_path}\n"
        f"Source scope: {cluster.source_scope}\n"
        f"Evidence count: {len(cluster.items)}\n"
        "Requirements:\n"
        "- One sentence.\n"
        "- Be generic and reusable.\n"
        "- No bullet list.\n"
        "- No markdown.\n"
        "- No hedging.\n"
        "Evidence:\n"
        f"{support_lines or '- No snippets available'}\n"
    )
    try:
        refined = (await generate(prompt)).strip()
        return refined or fallback
    except Exception:
        return fallback


def _observation_for_cluster(cluster: Cluster) -> str:
    return (
        f"Found {len(cluster.items)} {cluster.source_scope}-level records about '{cluster.topic_path}' "
        f"across {len(cluster.source_set)} distinct contexts with avg similarity {cluster.avg_similarity:.2f}."
    )


def _why_it_matters(cluster: Cluster) -> str:
    if cluster.target_scope == "principle":
        return (
            "These domain canonicals repeat the same reusable engineering lesson. "
            "Promoting them reduces duplication across adjacent technical domains."
        )
    if cluster.target_scope == "meta":
        return (
            "These principles converge into a cross-domain heuristic that should remain available "
            "even when project-specific context is sparse."
        )
    return (
        f"This knowledge appears repeatedly in different contexts. Promoting it to {cluster.target_scope} "
        "reduces retrieval noise and keeps reusable guidance available across projects."
    )


def _topic_paths_compatible(existing_topic: str | None, candidate_topic: str) -> bool:
    if not existing_topic:
        return True
    if existing_topic == candidate_topic:
        return True
    existing_parts = [part for part in existing_topic.split("/") if part]
    candidate_parts = [part for part in candidate_topic.split("/") if part]
    if not existing_parts or not candidate_parts:
        return True
    shared = min(len(existing_parts), len(candidate_parts), 2)
    return existing_parts[:shared] == candidate_parts[:shared]


def _canonical_tags(topic_path: str, scope: str) -> list[str]:
    return _unique_preserve_order([f"topic:{topic_path}", f"scope:{scope}", "canonical"])


def _candidate_revision_payload(
    candidate: CrystallizationCandidate,
    *,
    supports: list[str],
    now_iso: str,
) -> dict[str, Any]:
    return build_candidate_revision(
        base={},
        updates={
            "content": candidate.statement,
            "topic_path": candidate.topic_path,
            "supports": _unique_preserve_order(supports),
            "support_count": len(_unique_preserve_order(supports)),
            "confidence": candidate.confidence,
            "source_scope": candidate.source_scope,
            "observation": candidate.observation,
            "why_it_matters": candidate.why_it_matters,
            "updated_at": now_iso,
        },
        fields=CANONICAL_CANDIDATE_FIELDS,
        proposed_at=now_iso,
        proposed_at_field="updated_at",
    )


def _extract_candidate_revision(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    candidate = extract_prefixed_candidate(
        payload,
        fields=CANONICAL_CANDIDATE_FIELDS,
        required_field="content",
        defaults={
            "topic_path": payload.get("topic_path", ""),
            "supports": list(payload.get("supports") or []),
            "support_count": int(payload.get("support_count") or 0),
            "confidence": float(payload.get("confidence") or 0.0),
            "source_scope": payload.get("source_scope", ""),
            "observation": payload.get("observation", ""),
            "why_it_matters": payload.get("why_it_matters", ""),
            "updated_at": payload.get("updated_at", ""),
        },
    )
    if candidate is None:
        return None
    candidate["supports"] = list(candidate.get("supports") or [])
    candidate["support_count"] = int(candidate.get("support_count") or 0)
    candidate["confidence"] = float(candidate.get("confidence") or 0.0)
    candidate["status"] = "proposed"
    return candidate


def _candidate_revision_patch(candidate_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **prefixed_candidate_patch(candidate_payload, fields=CANONICAL_CANDIDATE_FIELDS),
        "status_reason": "Candidate canonical revision pending review",
    }


def _clear_candidate_revision_patch() -> dict[str, Any]:
    return clear_prefixed_candidate_patch(fields=CANONICAL_CANDIDATE_FIELDS)


async def _scroll_points(
    qdrant_client,
    collection: str,
    *,
    scopes: Optional[set[str]] = None,
    include_suppressed: bool = False,
    with_vectors: bool = True,
) -> list[Any]:
    from qdrant_client.http import models as qmodels

    should = None
    if scopes:
        should = [
            qmodels.FieldCondition(key="scope", match=qmodels.MatchValue(value=scope))
            for scope in scopes
        ]

    results_all: list[Any] = []
    offset = None
    while True:
        scroll_filter = qmodels.Filter(should=should) if should else None
        results, next_offset = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=scroll_filter,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        for result in results:
            payload = result.payload or {}
            if not payload.get("topic_path"):
                continue
            if payload.get("category", "general") in _SKIP_CATEGORIES:
                continue
            if memory_payload_should_be_excluded_from_learning(payload):
                continue
            if not include_suppressed and payload.get("suppressed"):
                continue
            if payload.get("merged_into") and not include_suppressed:
                continue
            results_all.append(result)
        if next_offset is None:
            break
        offset = next_offset
    return results_all


def _point_to_evidence(point: Any) -> MemoryEvidence:
    payload = point.payload or {}
    content = payload.get("content", "")
    return MemoryEvidence(
        memory_id=str(point.id),
        content=content[:400],
        topic_path=payload.get("topic_path", ""),
        scope=payload.get("scope", "project"),
        project=payload.get("project"),
        agent_id=payload.get("agent_id", ""),
        importance_score=float(payload.get("importance_score", 0.5)),
        access_count=int(payload.get("access_count", 0)),
        category=payload.get("category", "general"),
        tags=list(payload.get("tags", [])),
        supports=list(payload.get("supports", [])),
    )


async def _build_candidates_for_scope(
    *,
    points: list[Any],
    group_depth: int,
    min_supports: int,
    min_diversity: int,
    target_scope: str,
    source_scope: str,
    ollama_svc,
) -> list[CrystallizationCandidate]:
    groups: dict[str, list[Any]] = {}
    for point in points:
        payload = point.payload or {}
        topic_path = payload.get("topic_path", "")
        group_key = _topic_group(topic_path, group_depth, target_scope)
        groups.setdefault(group_key, []).append(point)

    candidates: list[CrystallizationCandidate] = []
    for topic_group, grouped_points in groups.items():
        if len(grouped_points) < min_supports:
            continue

        items = [_point_to_evidence(point) for point in grouped_points]
        vectors = [point.vector for point in grouped_points if point.vector]
        avg_similarity = _avg_cosine(vectors)
        if avg_similarity < CLUSTER_SIMILARITY_THRESHOLD:
            continue

        source_set = {
            _source_identity(item, source_scope)
            for item in items
            if _source_identity(item, source_scope)
        }
        if len(source_set) < min_diversity:
            continue

        topic_path = _derive_topic_path(topic_group, items, target_scope)
        cluster = Cluster(
            topic_path=topic_path,
            items=items,
            avg_similarity=avg_similarity,
            source_set=source_set,
            source_scope=source_scope,
            target_scope=target_scope,
        )
        cluster.confidence = _compute_confidence(
            cluster,
            min_supports=min_supports,
            min_diversity=min_diversity,
        )
        if cluster.confidence < CANDIDATE_CONFIDENCE_THRESHOLD:
            continue

        statement = await _refine_statement(cluster, ollama_svc)
        supports = _unique_preserve_order(
            [item.memory_id for item in items]
            + [support for item in items for support in item.supports]
        )
        candidates.append(
            CrystallizationCandidate(
                key=_candidate_key(topic_path, target_scope),
                topic_path=topic_path,
                target_scope=target_scope,
                statement=statement,
                observation=_observation_for_cluster(cluster),
                why_it_matters=_why_it_matters(cluster),
                supports=supports,
                confidence=cluster.confidence,
                project_diversity=len(source_set),
                evidence_count=len(items),
                source_scope=source_scope,
            )
        )
    return candidates


async def find_crystallization_candidates(
    qdrant_client,
    collection: str,
    ollama_svc,
) -> list[CrystallizationCandidate]:
    leaf_points = await _scroll_points(qdrant_client, collection, scopes=LEAF_SCOPES, with_vectors=True)
    domain_points = await _scroll_points(qdrant_client, collection, scopes={"domain"}, with_vectors=True)
    principle_points = await _scroll_points(qdrant_client, collection, scopes={"principle"}, with_vectors=True)

    logger.info(
        "Crystallization: scanning leaves=%d domains=%d principles=%d",
        len(leaf_points),
        len(domain_points),
        len(principle_points),
    )

    candidates: list[CrystallizationCandidate] = []
    candidates.extend(
        await _build_candidates_for_scope(
            points=leaf_points,
            group_depth=2,
            min_supports=MIN_SUPPORTS_FOR_DOMAIN,
            min_diversity=MIN_PROJECTS_FOR_DOMAIN,
            target_scope="domain",
            source_scope="project",
            ollama_svc=ollama_svc,
        )
    )
    candidates.extend(
        await _build_candidates_for_scope(
            points=domain_points,
            group_depth=1,
            min_supports=MIN_SUPPORTS_FOR_PRINCIPLE,
            min_diversity=MIN_DOMAINS_FOR_PRINCIPLE,
            target_scope="principle",
            source_scope="domain",
            ollama_svc=ollama_svc,
        )
    )
    candidates.extend(
        await _build_candidates_for_scope(
            points=principle_points,
            group_depth=1,
            min_supports=MIN_SUPPORTS_FOR_META,
            min_diversity=MIN_PRINCIPLES_FOR_META,
            target_scope="meta",
            source_scope="principle",
            ollama_svc=ollama_svc,
        )
    )

    deduped: dict[str, CrystallizationCandidate] = {}
    for candidate in candidates:
        existing = deduped.get(candidate.key)
        if existing is None or candidate.confidence > existing.confidence:
            deduped[candidate.key] = candidate

    output = list(deduped.values())
    logger.info("Crystallization: found %d candidates", len(output))
    return output


async def reconcile_canonical_lifecycle(qdrant_client, collection: str) -> dict[str, int]:
    now = datetime.now(timezone.utc).isoformat()
    canonicals = await _scroll_points(
        qdrant_client,
        collection,
        scopes=CANONICAL_SCOPES,
        include_suppressed=True,
        with_vectors=False,
    )

    summary = {"active": 0, "suppressed": 0, "updated": 0}
    for point in canonicals:
        payload = point.payload or {}
        scope = payload.get("scope", "domain")
        if payload.get("merged_into"):
            continue

        last_review_action = str(payload.get("last_review_action") or "").strip()
        manual_override_active = {"reactivate", "merge_target", "apply_candidate"}
        manual_override_suppressed = {"suppress", "merge_source"}
        if last_review_action in manual_override_active:
            next_status = "active"
            should_suppress = False
        elif last_review_action in manual_override_suppressed:
            next_status = "merged" if last_review_action == "merge_source" else "suppressed"
            should_suppress = True
        else:
            support_count = len(payload.get("supports") or [])
            confidence = float(payload.get("confidence", 0.0))
            should_suppress = (
                confidence < CANONICAL_SUPPRESS_THRESHOLD
                or support_count < _min_supports_for_scope(scope)
            )
            next_status = "suppressed" if should_suppress else "active"

        support_count = len(payload.get("supports") or [])
        current_status = payload.get("canonical_status") or ("suppressed" if payload.get("suppressed") else "active")
        current_suppressed = bool(payload.get("suppressed", False))
        if current_status != next_status or current_suppressed != should_suppress:
            await qdrant_client.set_payload(
                collection_name=collection,
                payload={
                    "canonical_status": next_status,
                    "suppressed": should_suppress,
                    "suppressed_at": now if should_suppress else None,
                    "reactivated_at": None if should_suppress else now,
                    "support_count": support_count,
                },
                points=[str(point.id)],
            )
            summary["updated"] += 1
        if next_status == "merged":
            summary["suppressed"] += 1
        else:
            summary[next_status] += 1
    return summary


def _merge_payload(existing_payload: dict[str, Any], candidate: CrystallizationCandidate, now_iso: str) -> dict[str, Any]:
    existing_confidence = float(existing_payload.get("confidence", 0.0))
    merged_supports = _unique_preserve_order(
        list(existing_payload.get("supports", [])) + list(candidate.supports)
    )
    chosen_content = existing_payload.get("content", "")
    if candidate.confidence >= existing_confidence or not chosen_content:
        chosen_content = candidate.statement
    return {
        "content": chosen_content,
        "scope": candidate.target_scope,
        "topic_path": candidate.topic_path,
        "supports": merged_supports,
        "support_count": len(merged_supports),
        "confidence": max(existing_confidence, candidate.confidence),
        "canonical_status": "active",
        "suppressed": False,
        "merged_into": None,
        "source_scope": candidate.source_scope,
        "importance_score": min(0.5 + max(existing_confidence, candidate.confidence) * 0.4, 0.95),
        "observation": candidate.observation,
        "why_it_matters": candidate.why_it_matters,
        "updated_at": now_iso,
        "tags": _canonical_tags(candidate.topic_path, candidate.target_scope),
    }


async def _sync_canonical_to_tree(topic_path: str, canonical_id: str) -> None:
    try:
        store = get_tree_store()
        node = store.get_by_topic_path(topic_path)
        if not node:
            return
        meta = node.get("meta_json") or {}
        canonical_ids = _unique_preserve_order(list(meta.get("canonical_memory_ids", [])) + [canonical_id])
        meta["canonical_memory_ids"] = canonical_ids
        if not meta.get("canonical_memory_id"):
            meta["canonical_memory_id"] = canonical_id
        store.update_node(node["id"], meta_json=meta)
    except Exception as exc:
        logger.debug("Canonical tree sync failed for %s: %s", topic_path, exc)


async def apply_crystallization(
    candidate: CrystallizationCandidate,
    qdrant_client,
    collection: str,
    ollama_svc,
) -> str:
    from qdrant_client.http import models as qmodels
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    statement = candidate.statement.strip() or _draft_statement(
        Cluster(
            topic_path=candidate.topic_path,
            items=[],
            avg_similarity=candidate.confidence,
            source_set=set(),
            source_scope=candidate.source_scope,
            target_scope=candidate.target_scope,
        )
    )
    vector = await ollama_svc.embed(statement)
    candidate.statement = statement

    existing_matches = await qdrant_client.search(
        collection_name=collection,
        query_vector=vector,
        query_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="scope", match=qmodels.MatchAny(any=[candidate.target_scope])),
            ]
        ),
        limit=12,
        with_payload=True,
        with_vectors=False,
    )

    compatible_matches = [
        match
        for match in existing_matches
        if match.score >= CANONICAL_MERGE_THRESHOLD
        and _topic_paths_compatible((match.payload or {}).get("topic_path"), candidate.topic_path)
    ]

    if compatible_matches:
        winner = compatible_matches[0]
        winner_id = str(winner.id)
        winner_payload = winner.payload or {}
        base_supports = list((winner_payload.get("candidate_supports") or winner_payload.get("supports") or []))
        candidate_payload = _candidate_revision_payload(
            candidate,
            supports=base_supports + list(candidate.supports),
            now_iso=now_iso,
        )
        patch = _candidate_revision_patch(candidate_payload)
        await qdrant_client.set_payload(
            collection_name=collection,
            payload=patch,
            points=[winner_id],
        )
        canonical_id = winner_id
    else:
        canonical_id = str(uuid4())
        payload = {
            "content": statement,
            "agent_id": "crystallization",
            "memory_type": "fact",
            "category": "canonical",
            "importance_score": min(0.5 + candidate.confidence * 0.4, 0.95),
            "timestamp": now_iso,
            "source": "crystallization",
            "tags": _canonical_tags(candidate.topic_path, candidate.target_scope),
            "access_count": 0,
            "session_id": None,
            "decay_rate": 0.0,
            "pinned": True,
            "last_access_ts": None,
            "last_decay_ts": None,
            "related_ids": [],
            "project": None,
            "expires_at": None,
            "topic_path": candidate.topic_path,
            "scope": candidate.target_scope,
            "supports": _unique_preserve_order(candidate.supports),
            "support_count": len(_unique_preserve_order(candidate.supports)),
            "canonical_id": None,
            "confidence": candidate.confidence,
            "canonical_status": "active",
            "suppressed": False,
            "merged_into": None,
            "source_scope": candidate.source_scope,
            "observation": candidate.observation,
            "why_it_matters": candidate.why_it_matters,
            **_clear_candidate_revision_patch(),
        }
        await qdrant_client.upsert(
            collection_name=collection,
            points=[qmodels.PointStruct(id=canonical_id, vector=vector, payload=payload)],
        )

    if not compatible_matches:
        for support_id in _unique_preserve_order(candidate.supports):
            try:
                await qdrant_client.set_payload(
                    collection_name=collection,
                    payload={"canonical_id": canonical_id},
                    points=[support_id],
                )
            except Exception as exc:
                logger.debug("Failed to back-link support %s: %s", support_id, exc)

    await _sync_canonical_to_tree(candidate.topic_path, canonical_id)
    await reconcile_canonical_lifecycle(qdrant_client, collection)

    logger.info(
        "Crystallization: upserted canonical %s (scope=%s topic=%s supports=%d)",
        canonical_id[:8],
        candidate.target_scope,
        candidate.topic_path,
        len(candidate.supports),
    )
    return canonical_id


async def list_canonicals(
    qdrant_client,
    collection: str,
    *,
    scopes: Optional[list[str]] = None,
    topic_prefix: Optional[str] = None,
    include_suppressed: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    scope_set = set(scopes or list(CANONICAL_SCOPES))
    points = await _scroll_points(
        qdrant_client,
        collection,
        scopes=scope_set,
        include_suppressed=include_suppressed,
        with_vectors=False,
    )

    items: list[dict[str, Any]] = []
    for point in points:
        payload = point.payload or {}
        topic_path = payload.get("topic_path", "")
        if topic_prefix and topic_path != topic_prefix and not topic_path.startswith(topic_prefix + "/"):
            continue
        record = _point_to_record(point)
        items.append(
            {
                "id": str(record.id),
                "topic_path": record.topic_path or "",
                "scope": record.scope,
                "content": record.content,
                "supports": record.supports,
                "support_count": len(record.supports),
                "confidence": float(payload.get("confidence", 0.0)),
                "suppressed": bool(payload.get("suppressed", False)),
                "canonical_status": payload.get("canonical_status") or "active",
                "merged_into": payload.get("merged_into"),
                "candidate_revision": _extract_candidate_revision(payload),
                "last_review_action": payload.get("last_review_action"),
                "last_reviewed_by": payload.get("last_reviewed_by"),
                "last_review_source": payload.get("last_review_source"),
                "last_reviewed_at": payload.get("last_reviewed_at"),
                "last_review_reason": payload.get("last_review_reason"),
                "project": record.project,
                "timestamp": record.timestamp.isoformat(),
            }
        )

    items.sort(
        key=lambda item: (
            SCOPE_ORDER.index(item["scope"]) if item["scope"] in SCOPE_ORDER else 999,
            item["topic_path"],
            -item["confidence"],
        )
    )
    return items[:limit]


def _canonical_item_from_point(point: Any) -> dict[str, Any]:
    payload = point.payload or {}
    record = _point_to_record(point)
    return {
        "id": str(record.id),
        "topic_path": record.topic_path or "",
        "scope": record.scope,
        "content": record.content,
        "supports": record.supports,
        "support_count": len(record.supports),
        "confidence": float(payload.get("confidence", 0.0)),
        "suppressed": bool(payload.get("suppressed", False)),
        "canonical_status": payload.get("canonical_status") or "active",
        "merged_into": payload.get("merged_into"),
        "candidate_revision": _extract_candidate_revision(payload),
        "last_review_action": payload.get("last_review_action"),
        "last_reviewed_by": payload.get("last_reviewed_by"),
        "last_review_source": payload.get("last_review_source"),
        "last_reviewed_at": payload.get("last_reviewed_at"),
        "last_review_reason": payload.get("last_review_reason"),
        "project": record.project,
        "timestamp": record.timestamp.isoformat(),
    }


async def get_canonical(
    qdrant_client,
    collection: str,
    *,
    canonical_id: str,
) -> dict[str, Any]:
    results = await qdrant_client.retrieve(
        collection_name=collection,
        ids=[canonical_id],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise ValueError("Canonical not found")
    payload = results[0].payload or {}
    if payload.get("scope") not in CANONICAL_SCOPES:
        raise ValueError("Memory is not a canonical")
    return _canonical_item_from_point(results[0])


async def get_knowledge_hierarchy(
    qdrant_client,
    collection: str,
    *,
    topic_prefix: Optional[str] = None,
    include_suppressed: bool = False,
    limit_per_scope: int = 50,
    reconcile: bool = False,
) -> dict[str, Any]:
    lifecycle = {"active": 0, "suppressed": 0, "updated": 0}
    if reconcile:
        lifecycle = await reconcile_canonical_lifecycle(qdrant_client, collection)

    items = await list_canonicals(
        qdrant_client,
        collection,
        topic_prefix=topic_prefix,
        include_suppressed=include_suppressed,
        limit=max(limit_per_scope * len(CANONICAL_SCOPES), limit_per_scope),
    )

    by_scope: dict[str, list[dict[str, Any]]] = {scope: [] for scope in CANONICAL_SCOPES}
    totals: dict[str, int] = {scope: 0 for scope in CANONICAL_SCOPES}
    for item in items:
        scope = item["scope"]
        if scope not in by_scope:
            continue
        totals[scope] += 1
        if len(by_scope[scope]) < limit_per_scope:
            by_scope[scope].append(item)

    return {
        "topic_prefix": topic_prefix,
        "scope_order": SCOPE_ORDER,
        "totals": totals,
        "by_scope": by_scope,
        "lifecycle": lifecycle,
    }


async def set_canonical_status(
    qdrant_client,
    collection: str,
    *,
    canonical_id: str,
    suppressed: bool,
    reason: Optional[str] = None,
    reviewed_by: str = "user",
    review_source: str = "inline_user_approval",
) -> dict[str, Any]:
    results = await qdrant_client.retrieve(
        collection_name=collection,
        ids=[canonical_id],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise ValueError("Canonical not found")
    payload = results[0].payload or {}
    if payload.get("scope") not in CANONICAL_SCOPES:
        raise ValueError("Memory is not a canonical")

    now_iso = datetime.now(timezone.utc).isoformat()
    next_status = "suppressed" if suppressed else "active"
    patch = {
        "suppressed": suppressed,
        "canonical_status": next_status,
        "status_reason": reason,
        "suppressed_at": now_iso if suppressed else None,
        "reactivated_at": None if suppressed else now_iso,
        "last_review_action": "suppress" if suppressed else "reactivate",
        "last_reviewed_by": reviewed_by,
        "last_review_source": review_source,
        "last_reviewed_at": now_iso,
        "last_review_reason": reason or None,
    }
    await qdrant_client.set_payload(
        collection_name=collection,
        payload=patch,
        points=[canonical_id],
    )
    await reconcile_canonical_lifecycle(qdrant_client, collection)
    return {
        "id": canonical_id,
        "suppressed": suppressed,
        "canonical_status": next_status,
        "reason": reason,
        "last_review_action": patch["last_review_action"],
        "last_reviewed_by": reviewed_by,
        "last_review_source": review_source,
        "last_reviewed_at": now_iso,
        "last_review_reason": reason or None,
    }


async def merge_canonicals(
    qdrant_client,
    collection: str,
    *,
    source_id: str,
    target_id: str,
    reviewed_by: str = "user",
    review_source: str = "inline_user_approval",
    reason: Optional[str] = None,
) -> dict[str, Any]:
    if source_id == target_id:
        raise ValueError("Source and target cannot be the same")

    results = await qdrant_client.retrieve(
        collection_name=collection,
        ids=[source_id, target_id],
        with_payload=True,
        with_vectors=False,
    )
    by_id = {str(point.id): point for point in results}
    if source_id not in by_id or target_id not in by_id:
        raise ValueError("Source or target canonical not found")

    source_payload = by_id[source_id].payload or {}
    target_payload = by_id[target_id].payload or {}
    if source_payload.get("scope") not in CANONICAL_SCOPES or target_payload.get("scope") not in CANONICAL_SCOPES:
        raise ValueError("Both memories must be canonicals")
    if source_payload.get("scope") != target_payload.get("scope"):
        raise ValueError("Canonicals must have the same scope to merge")
    if source_payload.get("candidate_content") or target_payload.get("candidate_content"):
        raise ValueError("Resolve pending canonical candidate revisions before merge")

    now_iso = datetime.now(timezone.utc).isoformat()
    source_topic = source_payload.get("topic_path", "") or ""
    target_topic = target_payload.get("topic_path", "") or ""
    merged_topic = target_topic or source_topic
    merged_supports = _unique_preserve_order(
        list(target_payload.get("supports", [])) + list(source_payload.get("supports", []))
    )
    merged_confidence = max(float(target_payload.get("confidence", 0.0)), float(source_payload.get("confidence", 0.0)))
    chosen_content = target_payload.get("content", "") or source_payload.get("content", "")

    now_iso = datetime.now(timezone.utc).isoformat()
    await qdrant_client.set_payload(
        collection_name=collection,
        payload={
            "content": chosen_content,
            "supports": merged_supports,
            "support_count": len(merged_supports),
            "confidence": merged_confidence,
            "topic_path": merged_topic,
            "canonical_status": "active",
            "suppressed": False,
            "merged_into": None,
            "updated_at": now_iso,
            "tags": _canonical_tags(merged_topic, target_payload.get("scope", "domain")),
            "last_review_action": "merge_target",
            "last_reviewed_by": reviewed_by,
            "last_review_source": review_source,
            "last_reviewed_at": now_iso,
            "last_review_reason": reason or None,
        },
        points=[target_id],
    )
    await qdrant_client.set_payload(
        collection_name=collection,
        payload={
            "suppressed": True,
            "canonical_status": "merged",
            "merged_into": target_id,
            "merged_at": now_iso,
            "last_review_action": "merge_source",
            "last_reviewed_by": reviewed_by,
            "last_review_source": review_source,
            "last_reviewed_at": now_iso,
            "last_review_reason": reason or None,
        },
        points=[source_id],
    )

    for support_id in merged_supports:
        try:
            await qdrant_client.set_payload(
                collection_name=collection,
                payload={"canonical_id": target_id},
                points=[support_id],
            )
        except Exception as exc:
            logger.debug("Failed to relink support %s after merge: %s", support_id, exc)

    await _sync_canonical_to_tree(merged_topic, target_id)
    await reconcile_canonical_lifecycle(qdrant_client, collection)
    return {
        "source_id": source_id,
        "target_id": target_id,
        "merged_support_count": len(merged_supports),
        "topic_path": merged_topic,
        "last_review_action": "merge",
        "last_reviewed_by": reviewed_by,
        "last_review_source": review_source,
        "last_reviewed_at": now_iso,
        "last_review_reason": reason or None,
    }


async def apply_canonical_candidate(
    qdrant_client,
    collection: str,
    *,
    canonical_id: str,
    ollama_svc,
    reviewed_by: str = "user",
    review_source: str = "inline_user_approval",
    reason: Optional[str] = None,
) -> dict[str, Any]:
    from qdrant_client.http import models as qmodels

    results = await qdrant_client.retrieve(
        collection_name=collection,
        ids=[canonical_id],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise ValueError("Canonical not found")
    payload = results[0].payload or {}
    if payload.get("scope") not in CANONICAL_SCOPES:
        raise ValueError("Memory is not a canonical")
    candidate = _extract_candidate_revision(payload)
    if not candidate:
        raise ValueError("No candidate revision for canonical")

    now_iso = datetime.now(timezone.utc).isoformat()
    applied = apply_candidate_fields(
        effective={
            "content": payload.get("content", ""),
            "topic_path": payload.get("topic_path", ""),
            "supports": list(payload.get("supports") or []),
            "support_count": int(payload.get("support_count") or len(payload.get("supports") or [])),
            "confidence": float(payload.get("confidence") or 0.0),
            "source_scope": payload.get("source_scope", ""),
            "observation": payload.get("observation", ""),
            "why_it_matters": payload.get("why_it_matters", ""),
            "updated_at": payload.get("updated_at", ""),
        },
        candidate=candidate,
        fields=CANONICAL_CANDIDATE_FIELDS,
    )
    supports = _unique_preserve_order(list(applied.get("supports") or []))
    content = str(applied.get("content") or "")
    topic_path = str(applied.get("topic_path") or "")
    scope = payload.get("scope", "domain")
    patch = {
        "content": content,
        "supports": supports,
        "support_count": len(supports),
        "confidence": float(applied.get("confidence") or payload.get("confidence", 0.0)),
        "topic_path": topic_path,
        "source_scope": applied.get("source_scope") or payload.get("source_scope", ""),
        "observation": applied.get("observation") or payload.get("observation", ""),
        "why_it_matters": applied.get("why_it_matters") or payload.get("why_it_matters", ""),
        "updated_at": now_iso,
        "tags": _canonical_tags(topic_path, scope),
        "canonical_status": "active",
        "suppressed": False,
        "status_reason": reason or "Candidate revision applied",
        "last_review_action": "apply_candidate",
        "last_reviewed_by": reviewed_by,
        "last_review_source": review_source,
        "last_reviewed_at": now_iso,
        "last_review_reason": reason or None,
        **_clear_candidate_revision_patch(),
    }
    vector = await ollama_svc.embed(content)
    await qdrant_client.set_payload(collection_name=collection, payload=patch, points=[canonical_id])
    await qdrant_client.update_vectors(
        collection_name=collection,
        points=[qmodels.PointVectors(id=canonical_id, vector=vector)],
    )
    for support_id in supports:
        try:
            await qdrant_client.set_payload(
                collection_name=collection,
                payload={"canonical_id": canonical_id},
                points=[support_id],
            )
        except Exception as exc:
            logger.debug("Failed to relink support %s after candidate apply: %s", support_id, exc)
    await _sync_canonical_to_tree(topic_path, canonical_id)
    await reconcile_canonical_lifecycle(qdrant_client, collection)
    return await get_canonical(qdrant_client, collection, canonical_id=canonical_id)


async def discard_canonical_candidate(
    qdrant_client,
    collection: str,
    *,
    canonical_id: str,
    reviewed_by: str = "user",
    review_source: str = "inline_user_approval",
    reason: Optional[str] = None,
) -> dict[str, Any]:
    results = await qdrant_client.retrieve(
        collection_name=collection,
        ids=[canonical_id],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise ValueError("Canonical not found")
    payload = results[0].payload or {}
    if payload.get("scope") not in CANONICAL_SCOPES:
        raise ValueError("Memory is not a canonical")
    if not payload.get("candidate_content"):
        raise ValueError("No candidate revision for canonical")

    patch = {
        **_clear_candidate_revision_patch(),
        "last_review_action": "discard_candidate",
        "last_reviewed_by": reviewed_by,
        "last_review_source": review_source,
        "last_reviewed_at": datetime.now(timezone.utc).isoformat(),
        "last_review_reason": reason or None,
        "status_reason": reason or "Candidate revision discarded",
    }
    await qdrant_client.set_payload(collection_name=collection, payload=patch, points=[canonical_id])
    return await get_canonical(qdrant_client, collection, canonical_id=canonical_id)
