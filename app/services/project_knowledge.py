"""
Project Knowledge Cache — component-level documentation stored in Qdrant.

RepRap principle: the project documents itself, enabling agents to understand
components instantly without re-reading code each session. Knowledge accumulated
here is reusable across projects.

Storage: dedicated Qdrant collection `project_docs`.
One point per component per project, with:
  - Semantic embedding for natural-language search
  - Structured payload (purpose, implementation, files, endpoints)
  - File hash for staleness detection
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from app.config import settings
from app.services.ollama_service import OllamaService

logger = logging.getLogger(__name__)

COLLECTION = "project_docs"
SEARCH_THRESHOLD = 0.45
SEARCH_LIMIT = 5


class ProjectKnowledgeService:
    """Vector-backed store for project component documentation."""

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
            for field in ["project_id", "component_id", "status"]:
                await self._q.create_payload_index(
                    collection_name=COLLECTION,
                    field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD,
                )
            logger.info("Created project_docs collection")

    # ── Hash ───────────────────────────────────────────────────────────────────

    def compute_hash(self, file_contents: list[str]) -> str:
        """SHA256 of sorted file contents → 16-char hex. Detects file changes."""
        h = hashlib.sha256()
        for c in sorted(file_contents):
            h.update(c.encode())
        return h.hexdigest()[:16]

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _build_embed_text(self, name: str, purpose: str, implementation: str,
                          endpoints: list[str], key_files: list[str]) -> str:
        parts = [
            f"Component: {name}",
            f"Purpose: {purpose}",
            f"Implementation: {implementation}",
        ]
        if endpoints:
            parts.append(f"Endpoints: {', '.join(endpoints)}")
        if key_files:
            parts.append(f"Files: {', '.join(key_files)}")
        return "\n".join(parts)

    async def _find_point(self, project_id: str, component_id: str
                          ) -> Optional[tuple[str, dict]]:
        """Return (point_id, payload) for existing component, or None."""
        results, _ = await self._q.scroll(
            collection_name=COLLECTION,
            scroll_filter=qm.Filter(must=[
                qm.FieldCondition(key="project_id", match=qm.MatchValue(value=project_id)),
                qm.FieldCondition(key="component_id", match=qm.MatchValue(value=component_id)),
            ]),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not results:
            return None
        return str(results[0].id), results[0].payload or {}

    # ── Write ──────────────────────────────────────────────────────────────────

    async def upsert_component(
        self,
        project_id: str,
        component_id: str,
        name: str,
        purpose: str,
        implementation: str,
        key_files: list[str],
        endpoints: list[str],
        status: str,
        file_hash: str,
        version_note: str = "",
    ) -> str:
        """Store or update a component doc. Returns point ID."""
        existing = await self._find_point(project_id, component_id)
        point_id = existing[0] if existing else str(uuid.uuid4())

        payload = {
            "project_id": project_id,
            "component_id": component_id,
            "name": name,
            "purpose": purpose,
            "implementation": implementation,
            "key_files": key_files,
            "endpoints": endpoints,
            "status": status,
            "file_hash": file_hash,
            "version_note": version_note,
            "category": "component_doc",
        }

        embed_text = self._build_embed_text(name, purpose, implementation, endpoints, key_files)
        vector = await self._ollama.embed(embed_text)

        await self._q.upsert(
            collection_name=COLLECTION,
            points=[qm.PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        logger.info("Upserted component %s/%s (hash=%s)", project_id, component_id, file_hash)
        return point_id

    # ── Read ───────────────────────────────────────────────────────────────────

    async def get_component(self, project_id: str, component_id: str) -> Optional[dict]:
        result = await self._find_point(project_id, component_id)
        return result[1] if result else None

    async def list_components(self, project_id: str) -> list[dict]:
        results, _ = await self._q.scroll(
            collection_name=COLLECTION,
            scroll_filter=qm.Filter(must=[
                qm.FieldCondition(key="project_id", match=qm.MatchValue(value=project_id)),
            ]),
            limit=500,
            with_payload=True,
            with_vectors=False,
        )
        return [r.payload for r in results if r.payload]

    async def search(self, project_id: str, query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
        """Semantic search across components of a project."""
        try:
            vector = await self._ollama.embed(query)
        except Exception as e:
            logger.warning("Embed failed for project search: %s", e)
            return []

        filt = qm.Filter(must=[
            qm.FieldCondition(key="project_id", match=qm.MatchValue(value=project_id)),
        ]) if project_id else None

        hits = await self._q.search(
            collection_name=COLLECTION,
            query_vector=vector,
            query_filter=filt,
            limit=limit,
            score_threshold=SEARCH_THRESHOLD,
            with_payload=True,
        )
        return [{**hit.payload, "_score": round(hit.score, 3)} for hit in hits]

    async def get_stale_components(self, project_id: str,
                                   current_hashes: dict[str, str]) -> list[str]:
        """Return component_ids whose file_hash differs from current_hashes."""
        stored = await self.list_components(project_id)
        stale = []
        for comp in stored:
            cid = comp.get("component_id", "")
            stored_hash = comp.get("file_hash", "")
            if cid in current_hashes and current_hashes[cid] != stored_hash:
                stale.append(cid)
        return stale
