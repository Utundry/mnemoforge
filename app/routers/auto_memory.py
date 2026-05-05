"""
Auto-memory extraction via a local LLM (e.g. qwen3:1.7b via Ollama).

Pipeline stages (inspired by LangMem/LlamaIndex):
  raw text → LLM extraction → candidate memories → optional review → persist

POST /auto/extract           — extract and store immediately (one-shot)
POST /auto/extract/preview   — extract candidates, store as drafts for review
POST /auto/draft/confirm     — promote draft memories to active
POST /auto/draft/discard     — delete draft memories
POST /auto/context           — retrieve memories for a query as a prompt block

Memory types: fact | preference | experience | task | context | profile | procedural
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import LayoutMemoryDep, OllamaDep, QdrantDep
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.services.embedding_gateway import embed_query, embed_text
from app.services.llm_gateway import get_cloud_gateway
from app.services.scoring_service import ScoringService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auto", tags=["auto-memory"])

# ── Config ─────────────────────────────────────────────────────────────────────

# Small local model for memory management tasks — fast and cheap
MANAGER_MODEL = "qwen3:1.7b"


# ── Schemas ────────────────────────────────────────────────────────────────────

class ExtractRequest(BaseModel):
    text: str = Field(..., description="Conversation snippet to extract memories from")
    agent_id: str = Field("default", description="Who these memories belong to")
    session_id: Optional[str] = None


class ExtractedMemory(BaseModel):
    content: str
    memory_type: str
    importance: float
    tags: list[str] = []


class ExtractResponse(BaseModel):
    stored: int
    skipped: int
    memories: list[ExtractedMemory]


class ContextRequest(BaseModel):
    query: str = Field(..., description="User's question or task")
    agent_id: Optional[str] = None
    limit: int = Field(5, ge=1, le=20)


class ContextResponse(BaseModel):
    memories: list[dict]
    prompt_block: str  # ready to inject into system prompt


class FixLayoutRequest(BaseModel):
    text: str = Field(..., description="Text to check and fix keyboard layout")
    agent_id: str = Field("layout-fixer", description="Namespace for learned corrections")
    learn: bool = Field(True, description="Store correction in memory for future use")


class FixLayoutResponse(BaseModel):
    original: str
    corrected: str
    was_fixed: bool
    direction: str  # "en->ru", "ru->en", or "none"
    memory_hits: int = 0  # words resolved from memory (not heuristics)


class FixLayoutFeedbackRequest(BaseModel):
    original: str = Field(..., description="Original wrong-layout text")
    corrected: str = Field(..., description="User-confirmed correct text")


# ── Layout Fixer ───────────────────────────────────────────────────────────────

_EN_TO_RU = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
)
_RU_TO_EN = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
)


_EN_COMMON_WORDS = frozenset(
    "the a an is are was were be been being have has had do does did will would could should "
    "may might shall can not no yes and or but if in on at to of for with by from up out it "
    "he she we you they i me him her us them his this that these those what who how when where "
    "all any some one two three get set run use make let go see know need want look just like "
    "good well also more about only then there their which into than them here now new just ".split()
)


def _looks_like_english(text: str) -> bool:
    """Return True if text contains enough common English words to be real English."""
    words = [w.strip(".,!?;:\"'()[]").lower() for w in text.split()]
    if not words:
        return False
    en_word_count = sum(1 for w in words if w in _EN_COMMON_WORDS)
    # 2+ common English words OR >30% of words are common English words → likely real English
    return en_word_count >= 2 or (len(words) >= 3 and en_word_count / len(words) > 0.3)


_RU_VOWELS = set("аеёиоуыэюя")  # only true vowels, not й/ь/ъ
_EN_VOWELS = set("aeiouy")


def _has_vowels_after_translate(word: str, table: dict) -> bool:
    """Return True if the word, after translation, contains at least one vowel."""
    converted = word.lower().translate(table)
    # Check Cyrillic vowels (en->ru direction) or Latin vowels (ru->en direction)
    return any(c in _RU_VOWELS or c in _EN_VOWELS for c in converted)


def _convert_word(word: str, table: dict) -> str:
    """Convert a single word using the translation table.

    Abbreviations (converted form has no vowels) are left unchanged —
    this handles MCP, API, URL, HTTP, etc. without any hardcoded list.
    """
    if not any(c.isalpha() for c in word):
        return word
    if not _has_vowels_after_translate(word, table):
        return word  # Looks like an abbreviation — skip
    return word.translate(table)


import re as _re

def _detect_direction(text: str) -> Optional[str]:
    """Return 'en->ru', 'ru->en', or None if layout looks correct."""
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return None
    latin_ratio = sum(1 for c in alpha_chars if c.isascii()) / len(alpha_chars)
    cyrillic_ratio = sum(1 for c in alpha_chars if not c.isascii()) / len(alpha_chars)
    if latin_ratio > 0.75 and cyrillic_ratio < 0.1:
        if _looks_like_english(text):
            return None
        return "en->ru"
    if cyrillic_ratio > 0.75 and latin_ratio < 0.1:
        return "ru->en"
    return None


def _fix_layout(text: str) -> FixLayoutResponse:
    """Synchronous heuristic-only fix (used internally and as fallback)."""
    if not text or len(text.strip()) < 2:
        return FixLayoutResponse(original=text, corrected=text, was_fixed=False, direction="none")
    direction = _detect_direction(text)
    if direction is None:
        return FixLayoutResponse(original=text, corrected=text, was_fixed=False, direction="none")
    table = _EN_TO_RU if direction == "en->ru" else _RU_TO_EN
    fixed = _re.sub(r"[^\W\d_]+", lambda m: _convert_word(m.group(), table), text)
    return FixLayoutResponse(original=text, corrected=fixed, was_fixed=(fixed != text), direction=direction)


async def _fix_layout_with_memory(
    text: str,
    layout_mem,  # LayoutMemoryService
    learn: bool,
) -> FixLayoutResponse:
    """Memory-aware layout fix: lookup known terms first, heuristics for the rest."""
    if not text or len(text.strip()) < 2:
        return FixLayoutResponse(original=text, corrected=text, was_fixed=False, direction="none")

    await layout_mem.ensure_collection()

    direction = _detect_direction(text)
    if direction is None:
        return FixLayoutResponse(original=text, corrected=text, was_fixed=False, direction="none")

    table = _EN_TO_RU if direction == "en->ru" else _RU_TO_EN
    memory_hits = 0
    result_parts: list[str] = []

    # Process token by token, preserving non-alpha separators
    tokens = _re.split(r"([^\W\d_]+)", text)  # alternates: [sep, word, sep, word, ...]
    original_words: list[str] = []
    fixed_words: list[str] = []

    for token in tokens:
        if not token or not token[0].isalpha():
            result_parts.append(token)
            continue

        # 1. Check memory first
        mem = await layout_mem.lookup(token)
        if mem is not None:
            memory_hits += 1
            if mem["action"] == "keep":
                result_parts.append(token)
                original_words.append(token)
                fixed_words.append(token)
            else:
                result_parts.append(mem["corrected"])
                original_words.append(token)
                fixed_words.append(mem["corrected"])
            continue

        # 2. Heuristic fallback
        converted = _convert_word(token, table)
        result_parts.append(converted)
        original_words.append(token)
        fixed_words.append(converted)

    corrected = "".join(result_parts)
    was_fixed = corrected != text

    # Auto-learn from this correction
    if learn and was_fixed:
        await layout_mem.store_diff(original_words, fixed_words)

    return FixLayoutResponse(
        original=text,
        corrected=corrected,
        was_fixed=was_fixed,
        direction=direction,
        memory_hits=memory_hits,
    )


# ── LLM call ───────────────────────────────────────────────────────────────────

async def _llm(prompt: str) -> str:
    """Call the configured LLM gateway for memory-management prompts."""
    import re

    text = await get_cloud_gateway().generate(
        prompt,
        task_type="memory_extraction",
        mode="economy",
        max_tokens=1200,
        temperature=0.0,
        timeout=120.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ── Prompts ────────────────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """\
/no_think
You are a memory extraction assistant. Read the conversation below and extract \
facts, preferences, decisions, or context worth remembering for future sessions.

Rules:
- Only extract concrete, reusable information (not greetings, filler, obvious things)
- Each memory must be self-contained (understandable without the conversation)
- Assign memory_type from: fact | preference | experience | task | context | profile | procedural
  * fact: objective facts, data, configurations
  * preference: user/agent preferences, styles, settings
  * experience: past events, what happened, outcomes
  * task: action items, TODOs, goals
  * context: background context for a project or domain
  * profile: who the user/agent is — role, skills, identity attributes
  * procedural: how-to knowledge, processes, algorithms, recipes
- Assign importance 0.0-1.0 (0.9+ = critical, 0.7 = useful, 0.5 = maybe useful)
- Skip importance < 0.5
- Return ONLY valid JSON, no explanation

Output format:
[
  {{"content": "...", "memory_type": "fact", "importance": 0.8, "tags": ["tag1"]}},
  ...
]

If nothing worth storing: return []

Conversation:
{text}

JSON output:"""

_CONTEXT_PROMPT = """\
/no_think
Given this user query, generate 2-4 short search phrases to find relevant memories.
Return ONLY a JSON array of strings, nothing else.

Query: {query}

JSON array:"""


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/fix-layout", response_model=FixLayoutResponse)
async def fix_layout(body: FixLayoutRequest, layout_mem: LayoutMemoryDep):
    """
    Self-learning keyboard layout fixer (ru <-> en).
    Checks vector memory for known corrections first, falls back to heuristics.
    Stores new corrections for future use when learn=True.
    """
    return await _fix_layout_with_memory(body.text, layout_mem, body.learn)


@router.post("/fix-layout/feedback", response_model=dict)
async def fix_layout_feedback(body: FixLayoutFeedbackRequest, layout_mem: LayoutMemoryDep):
    """
    User-confirmed correction signal. Teach the system what was wrong.
    Compares original and corrected word by word, stores each pair.
    Words that match in corrected = "keep as-is". Words that differ = "replace".
    """
    await layout_mem.ensure_collection()

    orig_words = _re.findall(r"[^\W\d_]+", body.original)
    corr_words = _re.findall(r"[^\W\d_]+", body.corrected)

    stored = 0
    for orig, corr in zip(orig_words, corr_words):
        action = "keep" if orig.lower() == corr.lower() else "replace"
        await layout_mem.store(orig, corr, action)
        stored += 1

    return {"stored": stored, "message": f"Learned {stored} word corrections"}


@router.post("/extract", response_model=ExtractResponse)
async def auto_extract(body: ExtractRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Send a conversation snippet to the local LLM, extract memories, store them.
    Designed to be called from a Claude Code hook after each conversation turn.
    """
    # Ask local LLM what to remember
    try:
        raw = await _llm(_EXTRACT_PROMPT.format(text=body.text[:4000]))
        # Strip markdown code fences if present
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        extracted: list[dict] = json.loads(raw)
    except Exception as e:
        logger.warning("LLM extraction failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Manager LLM error: {e}")

    stored, skipped = 0, 0
    results: list[ExtractedMemory] = []

    for item in extracted:
        importance = float(item.get("importance", 0.5))
        if importance < 0.5:
            skipped += 1
            continue

        try:
            mem_type = MemoryType(item.get("memory_type", "fact"))
        except ValueError:
            mem_type = MemoryType.fact

        memory = MemoryCreate(
            content=item["content"],
            agent_id=body.agent_id,
            memory_type=mem_type,
            importance_score=importance,
            tags=item.get("tags", []),
            source="auto-extract",
            session_id=body.session_id,
        )

        try:
            vector, embedding_meta = await embed_text(
                memory.content,
                primary=ollama,
                purpose="auto_extract_memory",
                fallback_reason="auto_extract_memory_embedding_unavailable",
            )
            memory.meta.update(embedding_meta)
            await qdrant.insert(memory, vector)
            stored += 1
            results.append(ExtractedMemory(
                content=memory.content,
                memory_type=mem_type.value,
                importance=importance,
                tags=memory.tags,
            ))
        except Exception as e:
            logger.warning("Failed to store extracted memory: %s", e)
            skipped += 1

    return ExtractResponse(stored=stored, skipped=skipped, memories=results)


@router.post("/context", response_model=ContextResponse)
async def auto_context(body: ContextRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Given a user query, retrieve the most relevant memories and return them
    as a formatted block ready to inject into a system prompt.
    """
    # Ask local LLM to generate search terms
    try:
        raw = await _llm(_CONTEXT_PROMPT.format(query=body.query[:1000]))
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        search_terms: list[str] = json.loads(raw)
    except Exception as e:
        logger.warning("LLM context generation failed, using raw query: %s", e)
        search_terms = [body.query]

    # Search for each term, collect unique results
    seen_ids: set[str] = set()
    all_results = []

    for term in search_terms[:3]:
        try:
            vector, _embedding_meta = await embed_query(
                term,
                primary=ollama,
                purpose="auto_context_search",
            )
            hits = await qdrant.search(
                vector=vector,
                agent_id=body.agent_id,
                limit=body.limit,
            )
            scorer = ScoringService()
            scored = scorer.rank(hits, body.limit)
            for r in scored:
                mid = str(r.memory.id)
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    all_results.append(r)
        except Exception as e:
            logger.warning("Search failed for term %r: %s", term, e)

    # Sort by score, take top N
    all_results.sort(key=lambda x: x.score, reverse=True)
    top = all_results[: body.limit]

    # Format as prompt block
    if top:
        lines = ["[Relevant context from memory]"]
        for r in top:
            m = r.memory
            lines.append(f"- [{m.memory_type.value}] {m.content}")
        prompt_block = "\n".join(lines)
    else:
        prompt_block = ""

    return ContextResponse(
        memories=[
            {
                "id": str(r.memory.id),
                "content": r.memory.content,
                "type": r.memory.memory_type.value,
                "score": round(r.score, 3),
            }
            for r in top
        ],
        prompt_block=prompt_block,
    )


# ── Staged extraction (preview → review → confirm) ─────────────────────────────

class PreviewRequest(BaseModel):
    text: str = Field(..., description="Conversation snippet to extract candidates from")
    agent_id: str = Field("default", description="Who these memories belong to")
    session_id: Optional[str] = None
    store_drafts: bool = Field(
        True,
        description="If true, store candidates as drafts (status=draft) for later confirm/discard. "
                    "If false, return candidates without storing anything.",
    )


class CandidateMemory(BaseModel):
    id: Optional[str] = None  # set when store_drafts=True
    content: str
    memory_type: str
    importance: float
    tags: list[str] = []


class PreviewResponse(BaseModel):
    candidates: list[CandidateMemory]
    stored_as_drafts: bool
    draft_count: int


class ConfirmRequest(BaseModel):
    draft_ids: list[str] = Field(..., description="Memory IDs to promote from draft to active")
    confirmed_by: str = Field("user", min_length=1, max_length=256)
    confirmation_source: str = Field("manual_draft_confirm", max_length=128)
    reason: str = Field("", max_length=1000)


class ConfirmResponse(BaseModel):
    confirmed: int
    failed: int


class DiscardRequest(BaseModel):
    draft_ids: list[str] = Field(..., description="Draft memory IDs to delete")
    discarded_by: str = Field("user", min_length=1, max_length=256)
    discard_source: str = Field("manual_draft_discard", max_length=128)
    reason: str = Field("", max_length=1000)


class DiscardResponse(BaseModel):
    discarded: int


@router.post("/extract/preview", response_model=PreviewResponse)
async def extract_preview(body: PreviewRequest, qdrant: QdrantDep, ollama: OllamaDep):
    """
    Stage 1: Extract memory candidates from text WITHOUT immediately persisting them.

    Returns a list of candidate memories for review. If store_drafts=True (default),
    each candidate is stored in Qdrant with status='draft' so you can inspect them,
    then call /auto/draft/confirm to promote or /auto/draft/discard to delete.

    This implements the review/approve flow for auto-extracted memories.
    """
    try:
        raw = await _llm(_EXTRACT_PROMPT.format(text=body.text[:4000]))
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        extracted: list[dict] = json.loads(raw)
    except Exception as e:
        logger.warning("LLM preview extraction failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Manager LLM error: {e}")

    candidates: list[CandidateMemory] = []

    for item in extracted:
        importance = float(item.get("importance", 0.5))
        if importance < 0.5:
            continue
        try:
            mem_type = MemoryType(item.get("memory_type", "fact"))
        except ValueError:
            mem_type = MemoryType.fact

        candidate = CandidateMemory(
            content=item["content"],
            memory_type=mem_type.value,
            importance=importance,
            tags=item.get("tags", []),
        )

        if body.store_drafts:
            try:
                from qdrant_client.http import models as qmodels
                from datetime import datetime, timezone
                import uuid as _uuid

                draft_id = str(_uuid.uuid4())
                vector, embedding_meta = await embed_text(
                    candidate.content,
                    primary=ollama,
                    purpose="auto_extract_draft",
                    fallback_reason="auto_extract_draft_embedding_unavailable",
                )
                now = datetime.now(timezone.utc)
                payload = {
                    "content": candidate.content,
                    "agent_id": body.agent_id,
                    "memory_type": mem_type.value,
                    "category": "draft",
                    "importance_score": importance,
                    "timestamp": now.isoformat(),
                    "source": "auto-extract-draft",
                    "tags": candidate.tags + ["draft"],
                    "access_count": 0,
                    "session_id": body.session_id,
                    "status": "draft",
                    "meta": embedding_meta,
                    "decay_rate": 3.0,  # drafts decay fast if not confirmed
                }
                await qdrant._client.upsert(
                    collection_name=qdrant._collection,
                    points=[qmodels.PointStruct(id=draft_id, vector=vector, payload=payload)],
                )
                candidate.id = draft_id
            except Exception as e:
                logger.warning("Failed to store draft candidate: %s", e)

        candidates.append(candidate)

    stored = sum(1 for c in candidates if c.id is not None)
    return PreviewResponse(
        candidates=candidates,
        stored_as_drafts=body.store_drafts,
        draft_count=stored,
    )


@router.post("/draft/confirm", response_model=ConfirmResponse)
async def confirm_drafts(body: ConfirmRequest, qdrant: QdrantDep):
    """
    Stage 2: Promote draft memories to active (remove draft status).

    Call this after reviewing /extract/preview results to persist the ones you want.
    """
    from qdrant_client.http import models as qmodels

    confirmed = 0
    failed = 0

    for draft_id in body.draft_ids:
        try:
            points = await qdrant._client.retrieve(
                collection_name=qdrant._collection,
                ids=[draft_id],
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                failed += 1
                continue
            payload = points[0].payload or {}
            if payload.get("status") != "draft" and payload.get("category") != "draft":
                failed += 1
                continue
            await qdrant._client.set_payload(
                collection_name=qdrant._collection,
                payload={
                    "status": None,
                    "category": "general",
                    "decay_rate": 1.0,
                    "confirmed_by": body.confirmed_by,
                    "confirmation_source": body.confirmation_source,
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                    "confirmation_reason": body.reason.strip() or None,
                },
                points=[draft_id],
            )
            confirmed += 1
        except Exception as e:
            logger.warning("Failed to confirm draft %s: %s", draft_id, e)
            failed += 1

    return ConfirmResponse(confirmed=confirmed, failed=failed)


@router.post("/draft/discard", response_model=DiscardResponse)
async def discard_drafts(body: DiscardRequest, qdrant: QdrantDep):
    """
    Discard draft memories — delete them without persisting.

    Use this to reject unwanted candidates from /extract/preview.
    """
    from app.services.event_emitter import emit
    from qdrant_client.http import models as qmodels

    discarded = 0
    for draft_id in body.draft_ids:
        try:
            points = await qdrant._client.retrieve(
                collection_name=qdrant._collection,
                ids=[draft_id],
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                continue
            payload = points[0].payload or {}
            if payload.get("status") != "draft" and payload.get("category") != "draft":
                continue
            await qdrant._client.delete(
                collection_name=qdrant._collection,
                points_selector=qmodels.PointIdsList(points=[draft_id]),
            )
            await emit(
                "user_feedback",
                agent_id=body.discarded_by,
                transport="api",
                context_signature="auto_memory:draft_discard",
                payload={
                    "action": "discard_draft",
                    "draft_id": draft_id,
                    "discarded_by": body.discarded_by,
                    "discard_source": body.discard_source,
                    "reason": body.reason,
                },
            )
            discarded += 1
        except Exception as e:
            logger.warning("Failed to discard draft %s: %s", draft_id, e)

    return DiscardResponse(discarded=discarded)
