"""
Layout correction memory — self-learning keyboard layout fixer.

Stores word-level correction prецeденты in Qdrant collection `layout_terms`.
Each point represents a known term with its preferred form.

Lookup flow:
  1. Embed the input word
  2. Search layout_terms by vector similarity
  3. If score > LOOKUP_THRESHOLD → apply stored action (keep / replace)
  4. Otherwise → fall back to heuristic conversion

Learning flow:
  - After each successful fix: store (original, corrected) pair
  - On feedback: store user-confirmed corrections, mark keep-as-is terms
"""
from __future__ import annotations

import uuid
import logging
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from app.config import settings
from app.services.embedding_gateway import embed_query, embed_text
from app.services.ollama_service import OllamaService

logger = logging.getLogger(__name__)

COLLECTION = "layout_terms"
LOOKUP_THRESHOLD = 0.88   # min cosine similarity to trust stored correction
MIN_COUNT_TO_TRUST = 1    # how many times seen before we trust the record


class LayoutMemoryService:
    """Vector-backed store for layout correction prецedenты."""

    def __init__(self, qdrant: AsyncQdrantClient, ollama: OllamaService) -> None:
        self._q = qdrant
        self._ollama = ollama

    # ── Setup ──────────────────────────────────────────────────────────────────

    async def ensure_collection(self) -> None:
        collections = await self._q.get_collections()
        names = [c.name for c in collections.collections]
        if COLLECTION not in names:
            await self._q.create_collection(
                collection_name=COLLECTION,
                vectors_config=qm.VectorParams(
                    size=settings.embedding_dimensions,
                    distance=qm.Distance.COSINE,
                ),
            )
            logger.info("Created layout_terms collection")

    # ── Lookup ─────────────────────────────────────────────────────────────────

    async def lookup(self, word: str) -> Optional[dict]:
        """
        Return correction record for word if confidence is high enough.

        Returns dict with keys: action ("keep" | "replace"), corrected (str), score (float)
        Returns None if no match found.
        """
        try:
            vector, _embedding_meta = await embed_query(
                word.lower(),
                primary=self._ollama,
                purpose="layout_term_lookup",
            )
        except Exception:
            return None

        try:
            hits = await self._q.search(
                collection_name=COLLECTION,
                query_vector=vector,
                limit=1,
                score_threshold=LOOKUP_THRESHOLD,
                with_payload=True,
            )
        except Exception:
            return None

        if not hits:
            return None

        hit = hits[0]
        payload = hit.payload or {}
        if payload.get("count", 0) < MIN_COUNT_TO_TRUST:
            return None

        return {
            "action": payload.get("action", "keep"),
            "corrected": payload.get("corrected", word),
            "score": hit.score,
        }

    # ── Store ──────────────────────────────────────────────────────────────────

    async def store(
        self,
        original: str,
        corrected: str,
        action: str,  # "keep" | "replace"
    ) -> None:
        """Store or update a correction record. Increments count on existing records."""
        try:
            vector, embedding_meta = await embed_text(
                original.lower(),
                primary=self._ollama,
                purpose="layout_term_store",
                fallback_reason="layout_term_store_embedding_unavailable",
            )
        except Exception:
            return

        # Check if we already have this word (by vector similarity)
        try:
            existing = await self._q.search(
                collection_name=COLLECTION,
                query_vector=vector,
                limit=1,
                score_threshold=0.98,  # very tight for upsert — must be same word
                with_payload=True,
            )
        except Exception:
            existing = []

        if existing:
            point_id = existing[0].id
            old_count = (existing[0].payload or {}).get("count", 1)
            await self._q.set_payload(
                collection_name=COLLECTION,
                payload={
                    "corrected": corrected,
                    "action": action,
                    "count": old_count + 1,
                    "meta": embedding_meta,
                },
                points=[point_id],
            )
        else:
            await self._q.upsert(
                collection_name=COLLECTION,
                points=[
                    qm.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=vector,
                        payload={
                            "original": original.lower(),
                            "corrected": corrected,
                            "action": action,
                            "count": 1,
                            "meta": embedding_meta,
                        },
                    )
                ],
            )

    # ── Batch store ────────────────────────────────────────────────────────────

    async def store_diff(self, original_words: list[str], fixed_words: list[str]) -> None:
        """Store correction prецedenты for each (original, fixed) word pair."""
        for orig, fixed in zip(original_words, fixed_words):
            if not orig.isalpha():
                continue
            if orig.lower() == fixed.lower():
                # Word was kept as-is — remember it as a "keep" term
                await self.store(orig, orig, action="keep")
            else:
                await self.store(orig, fixed, action="replace")
