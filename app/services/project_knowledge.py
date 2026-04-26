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
from app.services.component_docs_store import get_component_docs_store
from app.services.ollama_service import OllamaService

logger = logging.getLogger(__name__)

COLLECTION = "project_docs"
SEARCH_THRESHOLD = 0.45
SEARCH_LIMIT = 5


def _component_payload_from_row(row: dict | None, fallback: dict | None = None) -> Optional[dict]:
    if row:
        payload = {
            "project_id": row["project_id"],
            "component_id": row["component_id"],
            "name": row["name"],
            "purpose": row["purpose"],
            "implementation": row["implementation"],
            "key_files": row.get("key_files") or [],
            "endpoints": row.get("endpoints") or [],
            "status": row.get("status") or "",
            "file_hash": row.get("file_hash") or "",
            "version_note": row.get("version_note") or "",
            "snapshot": row.get("snapshot") or {},
            "category": "component_doc",
            "point_id": row["id"],
        }
        extra = row.get("extra_payload") or {}
        for key, value in extra.items():
            payload.setdefault(key, value)
        return payload
    if fallback:
        payload = dict(fallback)
        payload.setdefault("key_files", payload.get("key_files") or [])
        payload.setdefault("endpoints", payload.get("endpoints") or [])
        payload.setdefault("snapshot", payload.get("snapshot") or {})
        payload.setdefault("status", payload.get("status") or "")
        payload.setdefault("category", payload.get("category") or "component_doc")
        payload.setdefault("point_id", payload.get("point_id") or fallback.get("component_id"))
        return payload
    return None


class ProjectKnowledgeService:
    """Vector-backed store for project component documentation."""

    def __init__(self, qdrant: AsyncQdrantClient, ollama: OllamaService) -> None:
        self._q = qdrant
        self._ollama = ollama
        self._store = get_component_docs_store()

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
        snapshot: dict | None = None,
        extra_payload: dict | None = None,
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
        if snapshot:
            payload["snapshot"] = snapshot
            payload["source_mode"] = snapshot.get("source_mode") or "workspace"
            payload["repo"] = snapshot.get("repo") or ""
            payload["branch"] = snapshot.get("branch") or ""
            payload["commit_sha"] = snapshot.get("commit_sha") or ""
            payload["pr_ref"] = snapshot.get("pr_ref") or ""
        if extra_payload:
            payload.update(extra_payload)

        embed_text = self._build_embed_text(name, purpose, implementation, endpoints, key_files)
        vector = await self._ollama.embed(embed_text)

        await self._q.upsert(
            collection_name=COLLECTION,
            points=[qm.PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        logger.info("Upserted component %s/%s (hash=%s)", project_id, component_id, file_hash)
        await self._store.upsert_component(
            point_id=point_id,
            project_id=project_id,
            component_id=component_id,
            name=name,
            purpose=purpose,
            implementation=implementation,
            key_files=key_files,
            endpoints=endpoints,
            status=status,
            file_hash=file_hash,
            version_note=version_note,
            snapshot=snapshot,
            extra_payload=extra_payload,
        )
        return point_id

    async def delete_component(self, project_id: str, component_id: str) -> bool:
        """Delete one stored component. Returns True when a point was removed."""
        existing = await self._find_point(project_id, component_id)
        if not existing:
            return False
        await self._q.delete(
            collection_name=COLLECTION,
            points_selector=qm.PointIdsList(points=[existing[0]]),
        )
        logger.info("Deleted component %s/%s", project_id, component_id)
        await self._store.delete(existing[0])
        return True

    # ── Read ───────────────────────────────────────────────────────────────────

    async def get_component(self, project_id: str, component_id: str) -> Optional[dict]:
        row = await self._store.get_by_key(project_id, component_id)
        return _component_payload_from_row(row)

    async def list_components(self, project_id: str) -> list[dict]:
        rows = await self._store.list_by_project(project_id, limit=500)
        return [
            comp
            for row in rows
            if (comp := _component_payload_from_row(row))
        ]

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
        ids = [str(hit.id) for hit in hits]
        rows = await self._store.get_many(ids)
        results: list[dict] = []
        for hit in hits:
            point_id = str(hit.id)
            payload = _component_payload_from_row(rows.get(point_id), fallback=hit.payload or {})
            if not payload:
                continue
            payload["_score"] = round(hit.score or 0.0, 3)
            results.append(payload)
        return results

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
