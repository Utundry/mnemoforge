"""
AI directory watcher & auto-ingestion router.

POST /watcher/scan       — one-time scan of AI directories → ingest into Qdrant
POST /watcher/start      — start background file watcher
POST /watcher/stop       — stop background file watcher
GET  /watcher/status     — current watcher status
GET  /watcher/dirs       — list detected AI directories on this machine
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from qdrant_client.http import models as qmodels

from app.dependencies import OllamaDep, QdrantDep
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.core.path_security import is_path_allowed
from app.services.ai_dir_parser import ParsedChunk, default_ai_dirs, extract_jsonl_conversation, scan_directory
from app.services.embedding_gateway import embed_text
from app.services.watcher_service import _analyze_conversation, set_services as watcher_set_services
from app.services.watcher_service import watcher

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/watcher", tags=["ai-dir-watcher"])


def _make_source(prefix: str, path: str, max_len: int = 128) -> str:
    path_part = path[-(max_len - len(prefix)):] if len(path) > max_len - len(prefix) else path
    return prefix + path_part


async def _store(chunk: ParsedChunk, agent_id: str, ollama: OllamaDep, qdrant: QdrantDep) -> None:
    mem = MemoryCreate(
        content=chunk.content,
        agent_id=agent_id,
        memory_type=MemoryType.context,
        category=chunk.category,
        importance_score=chunk.importance,
        source=_make_source("watcher:", chunk.source_path),
        tags=chunk.tags + ([chunk.file_hash] if chunk.file_hash else []),
    )
    vector, embedding_meta = await embed_text(
        mem.content,
        primary=ollama,
        purpose="watcher_scan_chunk",
        fallback_reason="watcher_scan_chunk_embedding_unavailable",
    )
    mem.meta.update(embedding_meta)
    await qdrant.insert(mem, vector)


async def _existing_hashes_for(qdrant: QdrantDep, hashes: set[str]) -> set[str]:
    """
    Return subset of `hashes` that already exist in the collection.

    Uses a filtered scroll on tags (keyword array) instead of scanning the full collection.
    """
    if not hashes:
        return set()

    found: set[str] = set()
    hashes_list = sorted(hashes)
    BATCH = 256

    for i in range(0, len(hashes_list), BATCH):
        batch = hashes_list[i:i + BATCH]
        batch_set = set(batch)
        offset = None

        while True:
            points, next_offset = await qdrant._client.scroll(
                collection_name=qdrant._collection,
                offset=offset,
                limit=256,
                scroll_filter=qmodels.Filter(must=[
                    qmodels.FieldCondition(key="tags", match=qmodels.MatchAny(any=batch)),
                ]),
                with_payload=["tags"],
                with_vectors=False,
            )
            for p in points:
                for tag in p.payload.get("tags", []):
                    if tag in batch_set:
                        found.add(tag)
            if found.issuperset(batch_set):
                break
            if next_offset is None:
                break
            offset = next_offset

    return found


# ── Schemas ────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    dirs: Optional[list[str]] = Field(
        None,
        description="Directories to scan. If empty — auto-detect AI dirs (.claude, .codex, etc.)",
    )
    agent_id: str = Field("ai-dirs", description="Agent namespace for ingested memories")
    max_files: int = Field(500, ge=1, le=5000)
    dry_run: bool = Field(False, description="Parse but don't store — just report what would be ingested")


class ScanResponse(BaseModel):
    scanned_dirs: list[str]
    files_processed: int
    chunks_found: int
    chunks_stored: int
    skipped_duplicates: int
    categories: dict[str, int]


class WatchRequest(BaseModel):
    dirs: Optional[list[str]] = Field(None, description="Directories to watch (default: auto-detect)")
    agent_id: str = Field("ai-dirs")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/dirs")
async def list_ai_dirs():
    """List AI assistant directories detected on this machine."""
    found = default_ai_dirs()
    return {
        "detected": [str(p) for p in found],
        "count": len(found),
    }


@router.post("/scan", response_model=ScanResponse)
async def scan_ai_dirs(body: ScanRequest, ollama: OllamaDep, qdrant: QdrantDep):
    """
    One-time scan of AI assistant directories.
    Parses all supported files and ingests them into Qdrant.
    Skips files already in Qdrant (by file hash).
    """
    # Resolve directories
    watcher_set_services(qdrant, ollama)
    if body.dirs:
        dirs = [Path(d) for d in body.dirs]
    else:
        dirs = default_ai_dirs()

    if not dirs:
        raise HTTPException(
            status_code=404,
            detail="No AI directories found. Specify dirs manually.",
        )

    # Scan all directories
    all_chunks = []
    conversations = []
    scanned_dirs = []
    files_seen: set[str] = set()

    for d in dirs:
        if not is_path_allowed(d):
            logger.warning("Directory blocked by INGEST_ALLOWED_ROOTS: %s", d)
            continue
        if not d.exists():
            logger.warning("Directory not found: %s", d)
            continue
        scanned_dirs.append(str(d))
        chunks = scan_directory(d, max_files=body.max_files)
        all_chunks.extend(chunks)
        files_seen.update(c.source_path for c in chunks)
        for convo_path in d.rglob("*.jsonl"):
            if len(conversations) >= body.max_files:
                break
            if not convo_path.is_file():
                continue
            convo = extract_jsonl_conversation(convo_path)
            if convo is not None:
                conversations.append(convo)

    # Count categories
    categories: dict[str, int] = {}
    for c in all_chunks:
        categories[c.category] = categories.get(c.category, 0) + 1

    if body.dry_run:
        return ScanResponse(
            scanned_dirs=scanned_dirs,
            files_processed=len(files_seen),
            chunks_found=len(all_chunks),
            chunks_stored=0,
            skipped_duplicates=0,
            categories=categories,
        )

    # Ingest — skip duplicates by hash
    # Load existing hashes only for the files we are about to ingest (fast filtered scroll)
    wanted_hashes = {c.file_hash for c in all_chunks if c.file_hash}
    try:
        existing_hashes = await _existing_hashes_for(qdrant, wanted_hashes)
    except Exception as e:
        logger.warning("Could not query existing hashes: %s", e)
        existing_hashes = set()

    stored = 0
    skipped = 0
    to_store = []
    for chunk in all_chunks:
        if chunk.file_hash and chunk.file_hash in existing_hashes:
            skipped += 1
        else:
            to_store.append(chunk)

    # Run ingestion concurrently in small batches
    BATCH = 8
    for i in range(0, len(to_store), BATCH):
        batch = to_store[i:i + BATCH]
        results = await asyncio.gather(
            *[_store(c, body.agent_id, ollama, qdrant) for c in batch],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Chunk ingestion failed: %s", r)
            else:
                stored += 1

    for convo in conversations:
        if convo.file_hash and convo.file_hash in existing_hashes:
            continue
        try:
            await _analyze_conversation(convo, body.agent_id, transport="watcher_scan")
        except Exception as e:
            logger.warning("Conversation analysis failed for %s: %s", convo.source_path, e)

    return ScanResponse(
        scanned_dirs=scanned_dirs,
        files_processed=len(files_seen),
        chunks_found=len(all_chunks),
        chunks_stored=stored,
        skipped_duplicates=skipped,
        categories=categories,
    )


@router.post("/start")
async def start_watcher(body: WatchRequest, ollama: OllamaDep, qdrant: QdrantDep):
    """Start background file watcher. New/modified files are ingested automatically."""
    if watcher.running:
        return {"status": "already_running", **watcher.status()}

    from app.services import watcher_service
    watcher_service.set_services(qdrant, ollama)

    dirs = [Path(d) for d in body.dirs] if body.dirs else default_ai_dirs()
    if not dirs:
        raise HTTPException(status_code=404, detail="No AI directories found")

    watched = watcher.start(dirs, body.agent_id)
    return {
        "status": "started",
        "watched_dirs": watched,
        "agent_id": body.agent_id,
    }


@router.post("/stop")
async def stop_watcher():
    """Stop background file watcher."""
    if not watcher.running:
        return {"status": "not_running"}
    watcher.stop()
    return {"status": "stopped"}


@router.get("/status")
async def watcher_status():
    """Current watcher status and list of watched directories."""
    return watcher.status()
