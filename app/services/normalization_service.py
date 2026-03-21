"""
Semantic Adaptation Layer — glossary + error-pattern normalization.

Learns user/team-specific terminology, abbreviations, and recurring mistakes.
Before embedding or processing, normalizes text using stored term mappings.

Storage: Qdrant, category="glossary_term", decay_rate=0.0 (permanent facts)
Cache: in-memory, TTL=60s — changes propagate within a minute.

Pipeline:
  text → load glossary terms (cached) → apply substitutions → normalized text
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.services.ollama_service import OllamaService
    from app.services.qdrant_service import QdrantService

logger = logging.getLogger(__name__)

AGENT_ID_GLOBAL = "normalization-global"
CATEGORY = "glossary_term"


class NormalizationResult:
    def __init__(self, original: str, normalized: str, applied: list[dict]):
        self.original = original
        self.normalized = normalized
        self.applied = applied
        self.was_changed = original != normalized


class NormalizationService:
    """
    Applies per-agent glossary substitutions to text before embedding.

    Terms are loaded from Qdrant on first use and cached for CACHE_TTL seconds.
    Both agent-specific terms and global terms (agent_id=normalization-global)
    are applied. Longer terms are matched first to prevent partial-match shadowing.
    """

    CACHE_TTL = 60.0  # seconds

    def __init__(self) -> None:
        # agent_id -> (monotonic timestamp, [(term, expansion), ...])
        self._cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}

    async def _load_terms(
        self, agent_id: str, qdrant: "QdrantService"
    ) -> list[tuple[str, str]]:
        """Load glossary terms for agent_id + global terms from Qdrant."""
        from qdrant_client.http import models as qmodels

        terms: list[tuple[str, str]] = []

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
                        terms.append((term, expansion))
            except Exception as e:
                logger.warning(
                    "Failed to load normalization terms for agent '%s': %s", aid, e
                )

        # Longer terms first — prevents short terms from partially shadowing longer ones
        terms.sort(key=lambda t: len(t[0]), reverse=True)
        return terms

    async def get_terms(
        self, agent_id: str, qdrant: "QdrantService"
    ) -> list[tuple[str, str]]:
        now = time.monotonic()
        cached = self._cache.get(agent_id)
        if cached is not None and now - cached[0] < self.CACHE_TTL:
            return cached[1]

        terms = await self._load_terms(agent_id, qdrant)
        self._cache[agent_id] = (now, terms)
        return terms

    def _invalidate(self, agent_id: str) -> None:
        self._cache.pop(agent_id, None)
        self._cache.pop(AGENT_ID_GLOBAL, None)

    async def normalize(
        self, text: str, agent_id: str, qdrant: "QdrantService"
    ) -> NormalizationResult:
        """Apply glossary substitutions to text. Returns original if no terms match."""
        terms = await self.get_terms(agent_id, qdrant)
        normalized = text
        applied: list[dict] = []

        for term, expansion in terms:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            if pattern.search(normalized):
                normalized = pattern.sub(expansion, normalized)
                applied.append({"term": term, "expansion": expansion})

        return NormalizationResult(
            original=text, normalized=normalized, applied=applied
        )

    async def add_term(
        self,
        term: str,
        expansion: str,
        agent_id: str,
        qdrant: "QdrantService",
        ollama: "OllamaService",
        global_scope: bool = False,
    ) -> UUID:
        """Add a glossary term to Qdrant and invalidate cache."""
        from app.models.enums import MemoryType
        from app.models.memory import MemoryCreate

        scope_agent = AGENT_ID_GLOBAL if global_scope else agent_id
        content = f"{term} → {expansion}"
        vector = await ollama.embed(content)

        mem = MemoryCreate(
            content=content,
            agent_id=scope_agent,
            memory_type=MemoryType.fact,
            category=CATEGORY,
            importance_score=0.8,
            source="normalization",
            tags=["glossary", f"term:{term.lower()}", f"expansion:{expansion}"],
            decay_rate=0.0,  # vocabulary doesn't expire
        )
        memory_id = await qdrant.insert(mem, vector)
        self._invalidate(agent_id)
        return memory_id

    async def delete_term(
        self, memory_id: UUID, agent_id: str, qdrant: "QdrantService"
    ) -> None:
        """Delete a glossary term and invalidate cache."""
        await qdrant.delete(memory_id)
        self._invalidate(agent_id)


# Singleton — shared across all requests
_norm_svc = NormalizationService()
