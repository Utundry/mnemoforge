"""
Keyboard Layout Fixer — adaptive ru↔en correction with local LLM.

Pipeline:
  1. Bigram anomaly detection  → language signal from letter-pair frequencies (~0ms)
  2. Rule-based check          → combined confidence score                    (~0ms)
  3. If confidence ≥ RULE_THRESHOLD → return immediately (fast path)
  4. Else → search Qdrant for similar past corrections (few-shot examples)
          → call qwen3:1.7b with those examples                              (~2-5s)
          → store result in Qdrant for future learning

POST /layout/fix                — fix a single text
POST /layout/fix/batch          — fix multiple texts in one call
POST /layout/feedback           — confirm or reject a previous fix (teaches the system)
GET  /layout/stats              — correction statistics
GET  /layout/analyze            — bigram analysis for a text (debug/explain)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import OllamaDep, QdrantDep
from app.services.performance_tracker import get_tracker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/layout", tags=["layout-fixer"])

# ── Constants ──────────────────────────────────────────────────────────────────

MANAGER_MODEL = "qwen3:1.7b"
AGENT_ID = "layout-fixer"          # namespace in Qdrant
RULE_THRESHOLD = 0.85               # confidence ≥ this → skip LLM
FEW_SHOT_LIMIT = 6                  # max past examples to inject into prompt
MIN_SCORE = 0.65                    # min Qdrant similarity for few-shot examples

# ── Character maps ─────────────────────────────────────────────────────────────

_EN_TO_RU = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
)
_RU_TO_EN = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
)

# Common English words — presence means "this is intentional English, don't fix"
_EN_STOP_WORDS = frozenset(
    "the a an is are was were be been have has had do does did will would could should "
    "may might can not no yes and or but if in on at to of for with by from up out "
    "it he she we you they i me him her us them this that these those what who how "
    "when where all any some one two three get set run use make go see know need want "
    "just like good also more about only then there their which into than new here now "
    "its my your our their also very just much many well still just back after before "
    "http https www api mcp llm sql css js ts py git docker".split()
)


# ── Schemas ────────────────────────────────────────────────────────────────────

class FixRequest(BaseModel):
    text: str = Field(..., description="Text to check and fix")
    force_llm: bool = Field(False, description="Always use LLM even if rule is confident")
    agent_id: Optional[str] = Field(None, description="Caller agent — for per-agent learning")


class FixResponse(BaseModel):
    id: str                     # correction record ID (use for feedback)
    original: str
    corrected: str
    was_fixed: bool
    direction: str              # "en->ru" | "ru->en" | "none"
    confidence: float           # 0.0-1.0
    method: str                 # "rule" | "llm" | "rule+llm"
    few_shot_count: int = 0     # how many past examples LLM used


class BatchFixRequest(BaseModel):
    texts: list[str] = Field(..., max_length=50)
    agent_id: Optional[str] = None


class BatchFixResponse(BaseModel):
    results: list[FixResponse]
    fixed_count: int
    skipped_count: int


class FeedbackRequest(BaseModel):
    correction_id: str          # FixResponse.id
    confirmed: bool             # True = fix was correct, False = it was wrong
    correct_text: Optional[str] = None  # if confirmed=False, provide the real text


class StatsResponse(BaseModel):
    total_corrections: int
    confirmed: int
    rejected: int
    rule_based: int
    llm_based: int
    most_common_direction: str


# ── Bigram anomaly engine ──────────────────────────────────────────────────────
#
# Scoring principle:
#   Positive score → bigram is common in REAL English
#   Negative score → bigram is common when Russian is typed in EN layout (anomaly)
#   Zero / absent  → neutral / rare in both
#
# Russian-in-EN fingerprints come from the most frequent Russian bigrams
# transliterated through the EN keyboard layout:
#   "пр" → "gh",  "но" → "yj",  "на" → "yf",  "ни" → "yb"
#   "по" → "gj",  "во" → "dj",  "ро" → "hj",  "ра" → "hf"
#   "ка" → "rf",  "ла" → "kf",  "ал" → "fk",  "та" → "nf"
#   "тс" → "nc" (rare in EN boundary), "вс" → "dc", "де" → "lt"
#   "ть" → "nm",  "ши" → "ub",  "ел" → "tk",  "ло" → "kj"

_BIGRAM_WEIGHTS: dict[str, float] = {
    # ── Real English markers (positive) ──────────────────────
    "th": +3.5, "he": +2.5, "in": +2.0, "er": +2.0, "an": +2.0,
    "re": +2.0, "on": +1.5, "en": +1.5, "at": +1.5, "ou": +2.5,
    "ed": +1.5, "nd": +2.0, "st": +1.5, "or": +2.0, "ar": +1.5,
    "al": +1.5, "le": +1.5, "it": +1.5, "is": +1.5, "io": +1.5,
    "hi": +1.5, "ha": +1.5, "as": +1.5, "ti": +1.5, "ng": +2.0,
    "se": +1.5, "nt": +1.5, "ea": +2.0, "te": +1.5, "co": +1.0,
    "li": +1.5, "es": +1.0, "de": +1.0, "ro": +1.0, "ic": +1.0,
    "ne": +1.0, "ve": +1.0, "me": +1.0, "be": +1.0, "ma": +1.0,
    "ow": +1.5, "oo": +1.5, "ee": +1.5, "ly": +1.5, "ll": +1.5,
    "ch": +2.0, "sh": +2.0, "wh": +2.0, "ph": +1.5, "qu": +2.0,
    "ck": +1.5, "ss": +1.0, "tt": +1.0, "ff": +1.0, "all": +2.0,

    # ── Russian-in-EN layout fingerprints (negative) ──────────
    # Very strong signals — essentially impossible in real English
    "gj": -4.0,   # пo
    "hj": -4.0,   # рo
    "dj": -4.0,   # вo
    "yj": -4.0,   # нo
    "yf": -4.0,   # нa
    "yb": -3.5,   # нu
    "nm": -4.0,   # тb (ть)
    "kf": -4.0,   # лa
    "fk": -3.5,   # aл
    "nf": -3.5,   # тa
    "rf": -4.0,   # кa
    "hf": -3.5,   # рa
    "kj": -3.5,   # лo
    "gk": -3.5,   # пл
    "jl": -3.5,   # oд
    "jc": -3.5,   # oc
    "pf": -3.5,   # зa
    "ub": -3.0,   # шu
    "tk": -3.0,   # eл
    "lt": -3.0,   # дe
    "dc": -3.0,   # вc
    "nc": -2.5,   # тc (at word boundary but rare)
    "bk": -3.0,   # иk
    "rj": -3.5,   # кo
    "lj": -3.0,   # дo
    "fq": -3.0,   # aй
    "df": -2.5,   # вa
    "yt": -1.0,   # нe (ambiguous — "yet" exists, but deduct slightly)
    "gh": -1.5,   # пр (ambiguous — "ghost", "right" exist)
}

# Trigrams add even stronger signals
_TRIGRAM_WEIGHTS: dict[str, float] = {
    # English
    "the": +4.0, "and": +3.5, "ing": +3.0, "ion": +3.0, "tio": +2.5,
    "ent": +2.0, "hat": +2.0, "for": +2.0, "not": +2.0, "tha": +2.0,

    # Russian-in-EN (nearly impossible in English)
    "ghb": -5.0,  # при
    "rfr": -5.0,  # как
    "ltk": -5.0,  # дел
    "ltkf": -5.0, # дела
    "yj": -4.0,   # already in bigrams but reinforce
    "gjl": -5.0,  # под
    "nj": -2.0,   # то — ambiguous
    "cnj": -4.5,  # сто
    "hfy": -5.0,  # ран
    "hjl": -5.0,  # род
    "yfq": -5.0,  # най
    "byf": -4.5,  # ина
    "yjv": -5.0,  # ном
}


def _ngram_score(text: str) -> float:
    """
    Compute a language signal from bigram and trigram frequencies.

    Returns:
      > 0  → looks like real English
      < 0  → looks like Russian typed in EN layout
      ≈ 0  → ambiguous
    """
    t = text.lower()
    # Remove non-alpha for cleaner analysis
    t_alpha = "".join(c for c in t if c.isalpha() or c == " ")

    score = 0.0
    count = 0

    # Bigrams (within each word)
    for word in t_alpha.split():
        if len(word) < 2:
            continue
        for i in range(len(word) - 1):
            bg = word[i:i+2]
            if bg in _BIGRAM_WEIGHTS:
                score += _BIGRAM_WEIGHTS[bg]
                count += 1

        # Trigrams
        for i in range(len(word) - 2):
            tg = word[i:i+3]
            if tg in _TRIGRAM_WEIGHTS:
                score += _TRIGRAM_WEIGHTS[tg]
                count += 1

    return score / max(count, 1)


def _ngram_confidence(text: str) -> tuple[str, float]:
    """
    Use n-gram analysis to suggest layout direction and confidence.

    Returns (direction_hint, confidence):
      direction_hint: "en->ru" | "ru->en" | "none"
      confidence: 0.0-1.0
    """
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return "none", 0.5

    latin_ratio = sum(1 for c in alpha_chars if c.isascii()) / len(alpha_chars)

    # Only apply bigram analysis to Latin text (potential Russian-in-EN)
    if latin_ratio < 0.75:
        return "none", 0.3

    score = _ngram_score(text)

    if score <= -2.0:
        # Strong Russian-in-EN signal — high confidence
        confidence = min(0.95, 0.70 + abs(score) * 0.05)
        return "en->ru", round(confidence, 3)
    elif score >= 2.0:
        # Strong English signal — confident it's real English
        confidence = min(0.97, 0.75 + score * 0.04)
        return "none", round(confidence, 3)
    else:
        # Ambiguous zone — low confidence, let LLM decide
        return "none", 0.35


# ── Rule-based engine ──────────────────────────────────────────────────────────

def _rule_fix(text: str) -> tuple[str, str, float]:
    """
    Returns (corrected_text, direction, confidence).
    Uses character ratio + stop-word check + bigram/trigram anomaly detection.
    direction: "en->ru" | "ru->en" | "none"
    confidence: 0.0-1.0
    """
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars or len(text.strip()) < 2:
        return text, "none", 1.0

    total = len(alpha_chars)
    latin_count = sum(1 for c in alpha_chars if c.isascii())
    cyrillic_count = total - latin_count
    latin_ratio = latin_count / total
    cyrillic_ratio = cyrillic_count / total

    # Clearly Cyrillic — ru→en direction (rare use case)
    if cyrillic_ratio >= 0.85:
        return text.translate(_RU_TO_EN), "ru->en", 0.40

    # Mixed text — defer to ngram + LLM
    if 0.2 < latin_ratio < 0.75:
        ng_dir, ng_conf = _ngram_confidence(text)
        return text, "none", ng_conf  # still return original, LLM will decide

    # Mostly Latin text — three-signal analysis
    if latin_ratio >= 0.75:

        # Signal 1: stop-word check
        words = [w.strip(".,!?;:\"'()[]{}").lower() for w in text.split()]
        en_word_count = sum(1 for w in words if w in _EN_STOP_WORDS)
        has_en_stop_words = (
            en_word_count >= 2 or
            (len(words) >= 4 and en_word_count / len(words) > 0.3)
        )

        # Signal 2: bigram/trigram anomaly
        ng_dir, ng_conf = _ngram_confidence(text)
        ngram_says_ru = ng_dir == "en->ru"
        ngram_says_en = ng_dir == "none" and ng_conf >= 0.80

        # Signal 3: latin ratio strength
        ratio_confidence = 0.6 + (latin_ratio - 0.75) * 1.6  # 0.60→0.96

        # Decision logic — combine signals
        if has_en_stop_words and ngram_says_en:
            # Both signals say English → very confident, don't fix
            return text, "none", min(0.97, (0.92 + ng_conf) / 2)

        if has_en_stop_words and not ngram_says_ru:
            # Stop words say English, ngrams ambiguous → likely English
            return text, "none", 0.88

        if ngram_says_ru and not has_en_stop_words:
            # Ngrams strongly say Russian-in-EN, no English stop words → fix
            combined = min(0.97, (ng_conf + ratio_confidence) / 2)
            return text.translate(_EN_TO_RU), "en->ru", combined

        if ngram_says_ru and has_en_stop_words:
            # Conflicting signals → ambiguous, let LLM decide
            return text, "none", 0.35

        if not has_en_stop_words:
            # No English stop words, no strong ngram signal → weak Russian-in-EN signal
            return text.translate(_EN_TO_RU), "en->ru", ratio_confidence * 0.85

        return text, "none", 0.50

    return text, "none", 0.50


# ── LLM engine ────────────────────────────────────────────────────────────────

async def _llm_call(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{settings.ollama_base_url}/api/generate",
            json={"model": MANAGER_MODEL, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        text = r.json()["response"].strip()
        # Strip qwen3 <think>...</think> blocks
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return text


async def _search_examples(
    text: str, ollama: OllamaDep, qdrant: QdrantDep
) -> list[dict]:
    """Find similar past corrections from Qdrant to use as few-shot examples."""
    try:
        vector = await ollama.embed(text)
        from qdrant_client.http import models as qmodels
        hits = await qdrant._client.search(
            collection_name=qdrant._collection,
            query_vector=vector,
            query_filter=qmodels.Filter(
                must=[qmodels.FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value="layout-correction"),
                )]
            ),
            limit=FEW_SHOT_LIMIT + 2,
            with_payload=True,
        )
        examples = []
        for hit in hits:
            if hit.score < MIN_SCORE:
                continue
            p = hit.payload or {}
            examples.append({
                "original": p.get("original", ""),
                "corrected": p.get("corrected", ""),
                "direction": p.get("direction", "none"),
                "confirmed": p.get("confirmed", None),
            })
            if len(examples) >= FEW_SHOT_LIMIT:
                break
        return examples
    except Exception as e:
        logger.warning("Few-shot search failed: %s", e)
        return []


async def _llm_fix(
    text: str,
    rule_corrected: str,
    rule_direction: str,
    examples: list[dict],
) -> tuple[str, str, float]:
    """
    Ask qwen3:1.7b to decide the correct layout fix.
    Returns (corrected_text, direction, confidence).
    """
    # Build few-shot block
    few_shot_lines = []
    for ex in examples:
        status = ""
        if ex["confirmed"] is True:
            status = " [confirmed]"
        elif ex["confirmed"] is False:
            status = " [rejected]"
        if ex["direction"] == "none":
            few_shot_lines.append(f'  "{ex["original"]}" → unchanged{status}')
        else:
            few_shot_lines.append(
                f'  "{ex["original"]}" → "{ex["corrected"]}" ({ex["direction"]}){status}'
            )

    few_shot_block = (
        "\nPast corrections (use as reference):\n" + "\n".join(few_shot_lines)
        if few_shot_lines
        else ""
    )

    rule_hint = (
        f'\nRule-based suggestion: direction="{rule_direction}", result="{rule_corrected}"'
        if rule_direction != "none"
        else "\nRule-based: no fix suggested (looks like correct language)"
    )

    prompt = f"""/no_think
You are a keyboard layout fixer for Russian ↔ English.
Determine if the text was typed in the WRONG keyboard layout and fix it.

Rules:
- If mostly Latin but NOT real English words → likely Russian typed in EN layout → fix en→ru
- If mostly Cyrillic but asked to convert → fix ru→en
- If it is real English text (code, commands, technical terms) → leave unchanged
- Return ONLY valid JSON, no explanation
{few_shot_block}{rule_hint}

Text to analyze: "{text}"

Return JSON:
{{"action": "en->ru" | "ru->en" | "none", "corrected": "...", "confidence": 0.0-1.0}}

JSON:"""

    started_at = perf_counter()
    try:
        raw = await _llm_call(prompt)
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
        action = data.get("action", "none")
        corrected = data.get("corrected", text)
        confidence = float(data.get("confidence", 0.7))

        try:
            get_tracker().record(
                component=MANAGER_MODEL,
                task_type="layout_fix",
                success=True,
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
                metadata={
                    "action": action,
                    "confidence": confidence,
                    "few_shot_count": len(examples),
                    "rule_direction": rule_direction,
                },
            )
        except Exception as ee:
            logger.warning("layout_fixer tracker record failed (success): %s", ee)

        if action == "none":
            return text, "none", confidence
        return corrected, action, confidence
    except Exception as e:
        try:
            get_tracker().record(
                component=MANAGER_MODEL,
                task_type="layout_fix",
                success=False,
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
                metadata={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "few_shot_count": len(examples),
                    "rule_direction": rule_direction,
                },
            )
        except Exception as ee:
            logger.warning("layout_fixer tracker record failed (fail): %s", ee)
        logger.warning("LLM layout fix failed: %s — falling back to rule result", e)
        return rule_corrected, rule_direction, 0.5


# ── Qdrant storage ─────────────────────────────────────────────────────────────

async def _store_correction(
    correction_id: str,
    original: str,
    corrected: str,
    direction: str,
    confidence: float,
    method: str,
    agent_id: str,
    ollama: OllamaDep,
    qdrant: QdrantDep,
) -> None:
    """Store a layout correction in Qdrant for future few-shot learning."""
    try:
        content = (
            f'"{original}" → "{corrected}" ({direction})'
            if direction != "none"
            else f'"{original}" → unchanged (correct language)'
        )
        vector = await ollama.embed(original)  # embed original for similarity search

        from app.models.memory import MemoryCreate
        from app.models.enums import MemoryType

        mem = MemoryCreate(
            content=content,
            agent_id=AGENT_ID,
            memory_type=MemoryType.fact,
            category="layout-correction",
            importance_score=confidence,
            source=method,
            tags=[direction, method, agent_id or "unknown"],
        )
        # Override the auto-generated ID with our correction_id
        from qdrant_client.http import models as qmodels
        now = datetime.now(timezone.utc)
        payload = {
            "content": content,
            "agent_id": AGENT_ID,
            "memory_type": "fact",
            "category": "layout-correction",
            "importance_score": confidence,
            "timestamp": now.isoformat(),
            "source": method,
            "tags": [direction, method],
            # Extra fields for feedback
            "original": original,
            "corrected": corrected,
            "direction": direction,
            "confirmed": None,       # updated by /feedback endpoint
            "caller_agent": agent_id or "unknown",
        }
        await qdrant._client.upsert(
            collection_name=qdrant._collection,
            points=[qmodels.PointStruct(
                id=correction_id,
                vector=vector,
                payload=payload,
            )],
        )
    except Exception as e:
        logger.warning("Failed to store layout correction: %s", e)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/fix", response_model=FixResponse)
async def fix_layout(body: FixRequest, ollama: OllamaDep, qdrant: QdrantDep):
    """
    Fix keyboard layout errors with adaptive LLM fallback.
    Returns a correction ID that can be used to send feedback.
    """
    correction_id = str(uuid.uuid4())
    text = body.text.strip()

    # Step 1: rule-based pass
    rule_corrected, rule_direction, rule_confidence = _rule_fix(text)

    # Step 2: fast path — rule is confident enough
    if rule_confidence >= RULE_THRESHOLD and not body.force_llm:
        was_fixed = rule_direction != "none"
        corrected = rule_corrected if was_fixed else text

        await _store_correction(
            correction_id, text, corrected, rule_direction,
            rule_confidence, "rule", body.agent_id or "unknown", ollama, qdrant,
        )

        return FixResponse(
            id=correction_id,
            original=text,
            corrected=corrected,
            was_fixed=was_fixed,
            direction=rule_direction,
            confidence=round(rule_confidence, 3),
            method="rule",
        )

    # Step 3: LLM path — search for few-shot examples first
    examples = await _search_examples(text, ollama, qdrant)
    llm_corrected, llm_direction, llm_confidence = await _llm_fix(
        text, rule_corrected, rule_direction, examples
    )

    was_fixed = llm_direction != "none"
    corrected = llm_corrected if was_fixed else text
    method = "rule+llm" if rule_direction != "none" else "llm"

    await _store_correction(
        correction_id, text, corrected, llm_direction,
        llm_confidence, method, body.agent_id or "unknown", ollama, qdrant,
    )

    return FixResponse(
        id=correction_id,
        original=text,
        corrected=corrected,
        was_fixed=was_fixed,
        direction=llm_direction,
        confidence=round(llm_confidence, 3),
        method=method,
        few_shot_count=len(examples),
    )


@router.post("/fix/batch", response_model=BatchFixResponse)
async def fix_layout_batch(body: BatchFixRequest, ollama: OllamaDep, qdrant: QdrantDep):
    """Fix keyboard layout errors for multiple texts at once."""
    import asyncio
    tasks = [
        fix_layout(
            FixRequest(text=t, agent_id=body.agent_id),
            ollama, qdrant,
        )
        for t in body.texts
    ]
    results = await asyncio.gather(*tasks)
    fixed = sum(1 for r in results if r.was_fixed)
    return BatchFixResponse(
        results=list(results),
        fixed_count=fixed,
        skipped_count=len(results) - fixed,
    )


@router.post("/feedback")
async def layout_feedback(body: FeedbackRequest, qdrant: QdrantDep):
    """
    Confirm or reject a layout fix. Teaches the system for future corrections.
    """
    try:
        from qdrant_client.http import models as qmodels
        patch = {"confirmed": body.confirmed}
        if not body.confirmed and body.correct_text:
            patch["user_correction"] = body.correct_text
        await qdrant._client.set_payload(
            collection_name=qdrant._collection,
            payload=patch,
            points=[body.correction_id],
        )
        # Boost importance if confirmed, reduce if rejected
        importance = 0.9 if body.confirmed else 0.1
        await qdrant._client.set_payload(
            collection_name=qdrant._collection,
            payload={"importance_score": importance},
            points=[body.correction_id],
        )
        return {"status": "ok", "correction_id": body.correction_id, "confirmed": body.confirmed}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Correction not found: {e}")


@router.get("/analyze")
async def analyze_layout(text: str):
    """
    Explain why the layout fixer made its decision.
    Returns bigram scores, signals, and final decision — useful for debugging.
    """
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return {"error": "No alphabetic characters found"}

    total = len(alpha_chars)
    latin_ratio = sum(1 for c in alpha_chars if c.isascii()) / total
    ngram_raw = _ngram_score(text)
    ng_dir, ng_conf = _ngram_confidence(text)
    rule_corrected, rule_dir, rule_conf = _rule_fix(text)

    # Show which bigrams/trigrams fired
    t = text.lower()
    fired_bigrams = []
    fired_trigrams = []
    for word in "".join(c if c.isalpha() else " " for c in t).split():
        for i in range(len(word) - 1):
            bg = word[i:i+2]
            if bg in _BIGRAM_WEIGHTS and _BIGRAM_WEIGHTS[bg] != 0:
                fired_bigrams.append({"ngram": bg, "score": _BIGRAM_WEIGHTS[bg]})
        for i in range(len(word) - 2):
            tg = word[i:i+3]
            if tg in _TRIGRAM_WEIGHTS:
                fired_trigrams.append({"ngram": tg, "score": _TRIGRAM_WEIGHTS[tg]})

    words = [w.strip(".,!?;:\"'()[]{}").lower() for w in text.split()]
    en_stop_found = [w for w in words if w in _EN_STOP_WORDS]

    return {
        "text": text,
        "latin_ratio": round(latin_ratio, 3),
        "ngram_raw_score": round(ngram_raw, 3),
        "ngram_direction": ng_dir,
        "ngram_confidence": ng_conf,
        "en_stop_words_found": en_stop_found,
        "rule_direction": rule_dir,
        "rule_confidence": round(rule_conf, 3),
        "rule_corrected": rule_corrected,
        "fired_bigrams": sorted(fired_bigrams, key=lambda x: x["score"]),
        "fired_trigrams": sorted(fired_trigrams, key=lambda x: x["score"]),
        "verdict": "will_use_llm" if rule_conf < RULE_THRESHOLD else "fast_path",
    }


@router.get("/stats", response_model=StatsResponse)
async def layout_stats(qdrant: QdrantDep):
    """Statistics on layout corrections stored in memory."""
    try:
        from qdrant_client.http import models as qmodels
        result = await qdrant._client.scroll(
            collection_name=qdrant._collection,
            scroll_filter=qmodels.Filter(
                must=[qmodels.FieldCondition(
                    key="agent_id",
                    match=qmodels.MatchValue(value=AGENT_ID),
                )]
            ),
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
        points = result[0]
        total = len(points)
        confirmed = sum(1 for p in points if p.payload.get("confirmed") is True)
        rejected = sum(1 for p in points if p.payload.get("confirmed") is False)
        rule_based = sum(1 for p in points if p.payload.get("source") == "rule")
        llm_based = total - rule_based

        directions = [p.payload.get("direction", "none") for p in points if p.payload.get("direction") != "none"]
        most_common = max(set(directions), key=directions.count) if directions else "none"

        return StatsResponse(
            total_corrections=total,
            confirmed=confirmed,
            rejected=rejected,
            rule_based=rule_based,
            llm_based=llm_based,
            most_common_direction=most_common,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
