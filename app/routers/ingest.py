"""
Ingest local files from disk into memory.

POST /api/v1/ingest/file   — ingest a single file
POST /api/v1/ingest/dir    — ingest all supported files in a directory
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.core.path_security import allowed_roots, check_path_allowed, is_path_allowed
from app.services.file_parser import ParsedChunk, parse_file, scan_directory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingest"])


class IngestFileRequest(BaseModel):
    path: str = Field(..., description="Absolute or relative path to the file")
    cwd: Optional[str] = Field(None, description="Base directory used to resolve a relative path")
    agent_id: str
    memory_type: MemoryType = MemoryType.context
    category: str = "document"
    importance_score: float = Field(0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None


class IngestDirRequest(BaseModel):
    path: str = Field(..., description="Absolute or relative path to directory")
    cwd: Optional[str] = Field(None, description="Base directory used to resolve a relative path")
    agent_id: str
    memory_type: MemoryType = MemoryType.context
    category: str = "document"
    importance_score: float = Field(0.5, ge=0.0, le=1.0)
    extensions: list[str] = Field(default_factory=list, description="e.g. ['md','txt']; empty = all supported")
    recursive: bool = True
    tags: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None


class IngestResponse(BaseModel):
    inserted: int
    failed: int
    skipped: int
    files_processed: int


def _resolve_ingest_path(raw_path: str, cwd: Optional[str]) -> Path:
    raw = Path(raw_path).expanduser()
    if raw.is_absolute():
        return raw.resolve(strict=False)

    roots = allowed_roots()
    candidates: list[Path] = []
    seen: set[str] = set()

    def _add(candidate: Path) -> None:
        resolved = candidate.expanduser().resolve(strict=False)
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            candidates.append(resolved)

    if cwd:
        _add(Path(cwd) / raw)
    _add(Path.cwd() / raw)
    for root in roots:
        _add(root / raw)

    allowed_candidates = [
        candidate
        for candidate in candidates
        if not roots or any(candidate == root or candidate.is_relative_to(root) for root in roots)
    ]
    existing_allowed = [candidate for candidate in allowed_candidates if candidate.exists()]
    if len(existing_allowed) == 1:
        return existing_allowed[0]
    if len(existing_allowed) > 1:
        matches = ", ".join(str(candidate) for candidate in existing_allowed[:3])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Relative path is ambiguous: {raw_path}. Matching locations: {matches}",
        )

    existing_candidates = [candidate for candidate in candidates if candidate.exists()]
    if len(existing_candidates) == 1:
        return existing_candidates[0]
    if len(existing_candidates) > 1:
        if cwd and candidates and candidates[0] in existing_candidates:
            return candidates[0]
        matches = ", ".join(str(candidate) for candidate in existing_candidates[:3])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Relative path is ambiguous: {raw_path}. Matching locations: {matches}",
        )

    if allowed_candidates:
        return allowed_candidates[0]
    if candidates:
        return candidates[0]
    return raw.resolve(strict=False)


async def _ingest_chunks(
    chunks: list[ParsedChunk],
    request: Union[IngestFileRequest, IngestDirRequest],
    qdrant: QdrantDep,
    ollama: OllamaDep,
) -> tuple[int, int]:
    inserted = failed = 0
    for chunk in chunks:
        extra_tags = list(set(request.tags + chunk.tags))
        source = chunk.source_file
        if chunk.heading:
            source = f"{chunk.source_file}#{chunk.heading}"

        mem = MemoryCreate(
            content=chunk.content,
            agent_id=request.agent_id,
            memory_type=request.memory_type,
            category=request.category,
            importance_score=request.importance_score,
            source=source,
            tags=extra_tags,
            session_id=request.session_id,
        )
        try:
            vector = await ollama.embed(mem.content)
            await qdrant.insert(mem, vector)
            inserted += 1
        except Exception as e:
            logger.warning("Failed to insert chunk from '%s': %s", chunk.source_file, e)
            failed += 1
    return inserted, failed


@router.post("/file", response_model=IngestResponse)
async def ingest_file(body: IngestFileRequest, qdrant: QdrantDep, ollama: OllamaDep):
    p = _resolve_ingest_path(body.path, body.cwd)
    try:
        check_path_allowed(p)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {body.path}")
    if not p.is_file():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Not a file: {body.path}")

    chunks = parse_file(p)
    if not chunks:
        return IngestResponse(inserted=0, failed=0, skipped=1, files_processed=1)

    inserted, failed = await _ingest_chunks(chunks, body, qdrant, ollama)
    return IngestResponse(inserted=inserted, failed=failed, skipped=0, files_processed=1)


@router.post("/dir", response_model=IngestResponse)
async def ingest_dir(body: IngestDirRequest, qdrant: QdrantDep, ollama: OllamaDep):
    p = _resolve_ingest_path(body.path, body.cwd)
    try:
        check_path_allowed(p)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Directory not found: {body.path}")
    if not p.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Not a directory: {body.path}")

    files = scan_directory(p, extensions=body.extensions or None, recursive=body.recursive)
    blocked = 0
    allowed_files = []
    for f in files:
        if is_path_allowed(f):
            allowed_files.append(f)
        else:
            blocked += 1
    files = allowed_files
    if not files:
        return IngestResponse(inserted=0, failed=0, skipped=blocked, files_processed=0)

    total_inserted = total_failed = total_skipped = blocked
    # Convert dir request fields to file-request-compatible object
    file_req = IngestFileRequest(
        path=body.path,
        cwd=body.cwd,
        agent_id=body.agent_id,
        memory_type=body.memory_type,
        category=body.category,
        importance_score=body.importance_score,
        tags=body.tags,
        session_id=body.session_id,
    )

    for f in files:
        chunks = parse_file(f)
        if not chunks:
            total_skipped += 1
            continue
        # Override source path for each file
        file_req.path = str(f)
        ins, fail = await _ingest_chunks(chunks, file_req, qdrant, ollama)
        total_inserted += ins
        total_failed += fail

    return IngestResponse(
        inserted=total_inserted,
        failed=total_failed,
        skipped=total_skipped,
        files_processed=len(files),
    )
