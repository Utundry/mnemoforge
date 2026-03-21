from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from qdrant_client.http import models as qmodels

from app.dependencies import OllamaDep, QdrantDep
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.services.code_search_parser import parse_code_file, scan_code_directory

router = APIRouter(prefix="/code", tags=["code-search"])

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{2,}")


def _tokenize(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(value)}


def _normalize_rel_path(value: str) -> str:
    return value.replace("\\", "/")


def _lexical_score(query: str, payload: dict) -> float:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0.0

    path = str(payload.get("code_path", ""))
    symbol = str(payload.get("code_symbol", ""))
    content = str(payload.get("content", ""))
    haystack_tokens = _tokenize(" ".join([path, symbol, content[:1200]]))
    overlap = len(query_tokens & haystack_tokens)
    if overlap == 0:
        return 0.0

    score = overlap / len(query_tokens)
    if symbol and any(t in symbol.lower() for t in query_tokens):
        score += 0.2
    if path and any(t in path.lower() for t in query_tokens):
        score += 0.1
    return round(min(score, 1.0), 4)


class CodeIndexRequest(BaseModel):
    path: str
    agent_id: str = Field("code-search", max_length=256)
    extensions: list[str] = Field(default_factory=lambda: ["py", "md", "txt", "rst"])
    recursive: bool = True
    importance_score: float = Field(0.45, ge=0.0, le=1.0)
    session_id: Optional[str] = None


class CodeIndexResponse(BaseModel):
    inserted: int
    failed: int
    skipped: int
    files_processed: int
    chunks_processed: int


class CodeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    agent_id: str = Field("code-search", max_length=256)
    limit: int = Field(10, ge=1, le=50)
    path_prefix: Optional[str] = None
    language: Optional[str] = None
    search_root: Optional[str] = None  # filesystem path for ripgrep lexical pass
    expand_query: bool = False          # use LLM to expand query before embedding
    rerank: bool = False                # use LLM to rerank top results by relevance


class CodeSearchHit(BaseModel):
    id: str
    path: str
    symbol: str
    chunk_type: str
    language: str
    lexical_score: float
    semantic_score: float
    final_score: float
    snippet: str
    imports: list[str] = []


class CodeSearchResponse(BaseModel):
    hits: list[CodeSearchHit]
    total: int
    lexical_hits: int
    semantic_hits: int
    query_expanded: Optional[str] = None  # populated when expand_query=True
    reranked: bool = False                 # True when LLM reranking was applied


async def _lexical_file_hits(query: str, search_root: str) -> tuple[set[str], bool]:
    """Find files containing the query string using rg, grep, or signal unavailability.

    Returns (matched_paths, tool_available):
    - matched_paths: relative forward-slash paths of files that contain the query
    - tool_available: False means no external tool could run → caller should fall back
    """
    root = Path(search_root)

    async def _run(cmd: list[str]) -> set[str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        matched: set[str] = set()
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            p = Path(line.strip())
            try:
                matched.add(_normalize_rel_path(str(p.relative_to(root))))
            except ValueError:
                pass
        return matched

    # 1. Try ripgrep
    if shutil.which("rg"):
        try:
            hits = await _run([
                "rg", "--files-with-matches", "--ignore-case",
                "--glob", "*.{py,md,txt,rst,js,ts,tsx,jsx}",
                query, search_root,
            ])
            return hits, True
        except Exception:
            pass

    # 2. Try grep (available in Git Bash / Linux / macOS)
    if shutil.which("grep"):
        try:
            hits = await _run([
                "grep", "-ril",
                "--include=*.py", "--include=*.md", "--include=*.txt",
                "--include=*.rst", "--include=*.js", "--include=*.ts",
                query, search_root,
            ])
            return hits, True
        except Exception:
            pass

    # 3. No external tool available
    return set(), False


@router.post("/index", response_model=CodeIndexResponse)
async def index_codebase(body: CodeIndexRequest, qdrant: QdrantDep, ollama: OllamaDep):
    root = Path(body.path)
    if not root.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {body.path}")
    if not root.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Not a directory: {body.path}")

    files = scan_code_directory(root, extensions=body.extensions, recursive=body.recursive)
    inserted = failed = skipped = chunks_processed = 0

    for file_path in files:
        chunks = parse_code_file(file_path)
        if not chunks:
            skipped += 1
            continue

        rel_path = _normalize_rel_path(str(file_path.relative_to(root)))
        for chunk in chunks:
            if not chunk.content.strip():
                skipped += 1
                continue
            chunks_processed += 1
            mem = MemoryCreate(
                content=chunk.content,
                agent_id=body.agent_id,
                memory_type=MemoryType.context,
                category="code_component",
                importance_score=body.importance_score,
                source=f"code-index:{rel_path}",
                tags=[
                    "code",
                    f"language:{chunk.language}",
                    f"kind:{chunk.chunk_type}",
                    f"path:{rel_path}",
                    f"symbol:{chunk.symbol}",
                ],
                session_id=body.session_id,
                decay_rate=0.0,
            )
            try:
                imports_text = " ".join(chunk.imports[:20]) if chunk.imports else ""
                embed_text = (
                    f"{chunk.language} {chunk.chunk_type} {chunk.symbol} {rel_path}"
                    + (f" imports:{imports_text}" if imports_text else "")
                    + f"\n{chunk.content[:1200]}"
                )
                vector = await ollama.embed(embed_text)
                memory_id = await qdrant.insert(mem, vector)
                payload_extra: dict = {
                    "code_path": rel_path,
                    "code_symbol": chunk.symbol,
                    "code_chunk_type": chunk.chunk_type,
                    "code_language": chunk.language,
                }
                if chunk.imports:
                    payload_extra["code_imports"] = chunk.imports[:30]
                await qdrant._client.set_payload(
                    collection_name=qdrant._collection,
                    payload=payload_extra,
                    points=[str(memory_id)],
                )
                # Dual-write content + metadata to SQLite
                from app.services.memory_store import get_memory_store
                await get_memory_store().upsert(
                    str(memory_id), "code_component", chunk.content,
                    {
                        "code_path": rel_path,
                        "code_symbol": chunk.symbol,
                        "code_chunk_type": chunk.chunk_type,
                        "code_language": chunk.language,
                        "code_imports": chunk.imports[:30] if chunk.imports else [],
                    },
                )
                inserted += 1
            except Exception:
                failed += 1

    return CodeIndexResponse(
        inserted=inserted,
        failed=failed,
        skipped=skipped,
        files_processed=len(files),
        chunks_processed=chunks_processed,
    )


@router.post("/search", response_model=CodeSearchResponse)
async def search_code(body: CodeSearchRequest, qdrant: QdrantDep, ollama: OllamaDep):
    # 1. Optional query expansion via LLM
    effective_query = body.query
    query_expanded: str | None = None
    if body.expand_query:
        expanded = await ollama.generate(
            f"Expand this code search query with 3-5 related technical terms. "
            f"Return only the expanded query on one line:\n{body.query}"
        )
        if expanded:
            effective_query = expanded
            query_expanded = expanded

    # 2. Build Qdrant filter
    must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="code_component")),
        qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=body.agent_id)),
    ]
    if body.language:
        must.append(qmodels.FieldCondition(key="code_language", match=qmodels.MatchValue(value=body.language)))
    query_filter = qmodels.Filter(must=must)

    # 3. Semantic retrieval
    vector = await ollama.embed(effective_query)
    semantic_points = await qdrant._client.search(
        collection_name=qdrant._collection,
        query_vector=vector,
        query_filter=query_filter,
        limit=body.limit * 4,
        with_payload=True,
    )

    merged: dict[str, dict] = {}
    semantic_hits = 0
    lexical_hits = 0

    for point in semantic_points:
        payload = point.payload
        path = str(payload.get("code_path", ""))
        if body.path_prefix and not path.startswith(body.path_prefix):
            continue
        semantic_hits += 1
        merged[str(point.id)] = {
            "payload": payload,
            "semantic_score": float(point.score),
            "lexical_score": 0.0,
        }

    # 4. Lexical pass — ripgrep when search_root provided, else Python token scoring
    scroll_points, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=query_filter,
        limit=300,
        with_payload=True,
        with_vectors=False,
    )

    use_token_scoring = True
    if body.search_root:
        file_hits, tool_available = await _lexical_file_hits(effective_query, body.search_root)
        if tool_available:
            use_token_scoring = False
            # Boost already-merged semantic hits whose file matched
            for entry in merged.values():
                path = str(entry["payload"].get("code_path", ""))
                if path in file_hits and entry["lexical_score"] == 0.0:
                    entry["lexical_score"] = 1.0
                    lexical_hits += 1
            # Add scroll hits whose file matched but weren't in semantic results
            for point in scroll_points:
                pid = str(point.id)
                if pid in merged:
                    continue
                path = str(point.payload.get("code_path", ""))
                if body.path_prefix and not path.startswith(body.path_prefix):
                    continue
                if path in file_hits:
                    lexical_hits += 1
                    merged[pid] = {
                        "payload": point.payload,
                        "semantic_score": 0.0,
                        "lexical_score": 1.0,
                    }

    if use_token_scoring:
        for point in scroll_points:
            payload = point.payload
            path = str(payload.get("code_path", ""))
            if body.path_prefix and not path.startswith(body.path_prefix):
                continue
            lexical = _lexical_score(body.query, payload)
            if lexical <= 0:
                continue
            lexical_hits += 1
            entry = merged.setdefault(
                str(point.id),
                {"payload": payload, "semantic_score": 0.0, "lexical_score": 0.0},
            )
            entry["lexical_score"] = max(entry["lexical_score"], lexical)

    # 4b. Hydrate from SQLite (content + imports; Qdrant payload is fallback)
    from app.services.memory_store import get_memory_store
    _store_data = await get_memory_store().get_many(list(merged.keys()))
    for pid, entry in merged.items():
        sd = _store_data.get(pid)
        if sd:
            meta = sd.get("metadata", {})
            entry["payload"]["content"] = sd.get("content") or entry["payload"].get("content", "")
            if meta.get("code_imports"):
                entry["payload"]["code_imports"] = meta["code_imports"]

    # 5. Rank and return
    ranked: list[CodeSearchHit] = []
    for point_id, entry in merged.items():
        payload = entry["payload"]
        semantic_score = round(entry["semantic_score"], 4)
        lexical_score = round(entry["lexical_score"], 4)
        final_score = round((semantic_score * 0.65) + (lexical_score * 0.35), 4)
        ranked.append(
            CodeSearchHit(
                id=point_id,
                path=str(payload.get("code_path", "")),
                symbol=str(payload.get("code_symbol", "")),
                chunk_type=str(payload.get("code_chunk_type", "")),
                language=str(payload.get("code_language", "")),
                lexical_score=lexical_score,
                semantic_score=semantic_score,
                final_score=final_score,
                snippet=str(payload.get("content", ""))[:400],
                imports=payload.get("code_imports", []),
            )
        )

    ranked.sort(key=lambda hit: (hit.final_score, hit.lexical_score, hit.semantic_score), reverse=True)
    hits = ranked[: body.limit]

    # 6. Optional LLM reranking of top results
    reranked = False
    if body.rerank and len(hits) > 1:
        hits, reranked = await _llm_rerank(body.query, hits, ollama)

    return CodeSearchResponse(
        hits=hits,
        total=len(hits),
        lexical_hits=lexical_hits,
        semantic_hits=semantic_hits,
        query_expanded=query_expanded,
        reranked=reranked,
    )


async def _llm_rerank(
    query: str, hits: list[CodeSearchHit], ollama
) -> tuple[list[CodeSearchHit], bool]:
    """Ask the LLM to reorder hits by relevance to the query.

    Returns (reordered_hits, was_reranked). Falls back to original order on failure.
    The LLM is shown numbered snippets and asked to return a ranked list of indices.
    """
    numbered = "\n\n".join(
        f"[{i + 1}] {h.symbol} ({h.path})\n{h.snippet[:300]}"
        for i, h in enumerate(hits)
    )
    prompt = (
        f"You are a code search assistant. Given the query and code snippets below, "
        f"return a comma-separated list of snippet numbers ordered from most to least relevant. "
        f"Return ONLY numbers separated by commas, nothing else.\n\n"
        f"Query: {query}\n\n"
        f"Snippets:\n{numbered}"
    )
    response = await ollama.generate(prompt)
    if not response:
        return hits, False

    # Parse "3, 1, 2" or "3,1,2"
    try:
        indices = [int(x.strip()) - 1 for x in response.split(",") if x.strip().isdigit()]
        seen: set[int] = set()
        reordered: list[CodeSearchHit] = []
        for idx in indices:
            if 0 <= idx < len(hits) and idx not in seen:
                reordered.append(hits[idx])
                seen.add(idx)
        # Append any hits the LLM omitted (preserve semantic order for remainder)
        for i, hit in enumerate(hits):
            if i not in seen:
                reordered.append(hit)
        return reordered, True
    except Exception:
        return hits, False
