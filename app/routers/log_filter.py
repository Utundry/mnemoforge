"""
LLM-moderated log filter.

Three-stage pipeline:
  Stage 1 (~0ms)   — Regex pre-filter: obvious noise removed instantly
  Stage 2 (~0ms)   — Deduplication: repeated lines collapsed with count
  Stage 3 (~5-30s) — qwen3:1.7b semantic refinement on ambiguous lines
                     Uses Qdrant to learn project-specific noise patterns

POST /log/filter        — filter log from text or file path
POST /log/feedback      — mark a kept line as noise (teaches future filters)
GET  /log/patterns      — list learned noise patterns for a project
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep
from app.services.embedding_gateway import embed_text
from app.services.llm_gateway import get_cloud_gateway

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/log", tags=["log-filter"])

MANAGER_MODEL = "qwen3:1.7b"
AGENT_ID = "log-filter"
CHUNK_SIZE = 40          # lines per LLM call
CONTEXT_LINES = 5        # lines before/after each kept line
MAX_LINE_LEN = 300       # truncate very long lines for LLM
MAX_INPUT_LINES = 200_000

# ── Stage 1: Regex filters ─────────────────────────────────────────────────────

# Always KEEP these patterns
_KEEP_RE = re.compile(
    r"("
    r"\b(ERROR|FATAL|CRITICAL|EXCEPTION|FAIL(?:ED|URE)?|PANIC)\b"
    r"|Traceback \(most recent call last\)"
    r"|\bstack\s*trace\b"
    r"|^\s+(?:at |File \"|in <)"          # stack frame lines
    r"|\bConnectionRefused|\bTimeout\b|\bEOFError\b"
    r"|\bOutOfMemory|OOMKilled"
    r"|\bAssertionError|\bTypeError|\bValueError|\bKeyError|\bIndexError"
    r"|\bSegmentation fault|\bcore dumped"
    r"|\b[45]\d\d\b"                       # HTTP 4xx / 5xx
    r"|\bKilled\b|\bAborted\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Always SKIP these patterns (before LLM)
_SKIP_RE = re.compile(
    r"("
    r"^\s*$"                               # empty lines
    r"|\b(DEBUG|TRACE|VERBOSE)\b"
    r"|\bheartbeat\b|\bping\b|\bkeep.?alive\b"
    r"|\bcache (hit|miss|get|set|evict)\b"
    r"|\bhealth.?check\b"
    r"|\brequest (received|processed|handled)\b"
    r"|\bGET /health\b|\bGET /ping\b|\bGET /metrics\b"
    r"|\bconnection (opened|closed|established)\b"
    r"|\bINFO\b.{0,40}\b(ok|done|ready|started|stopped|complete)\b"
    r")",
    re.IGNORECASE,
)


def _stage1_regex(lines: list[str], focus: Optional[str]) -> tuple[set[int], set[int]]:
    """
    Returns (keep_indices, skip_indices).
    Lines not in either set are 'ambiguous' → go to LLM.
    """
    keep: set[int] = set()
    skip: set[int] = set()
    focus_re = re.compile(re.escape(focus), re.IGNORECASE) if focus else None

    for i, line in enumerate(lines):
        if focus_re and focus_re.search(line):
            keep.add(i)
            continue
        if _KEEP_RE.search(line):
            keep.add(i)
        elif _SKIP_RE.search(line):
            skip.add(i)

    return keep, skip


# ── Stage 2: Deduplication ─────────────────────────────────────────────────────

def _normalize_line(line: str) -> str:
    """Strip timestamps, UUIDs, IDs for deduplication key."""
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", "<TS>", line)
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<UUID>", s, flags=re.I)
    s = re.sub(r"\b\d{10,}\b", "<ID>", s)
    s = re.sub(r"\b\d+\.\d+\.\d+\.\d+\b", "<IP>", s)
    s = re.sub(r"\b0x[0-9a-f]+\b", "<HEX>", s, flags=re.I)
    return s.strip()


def _stage2_dedup(
    lines: list[str], ambiguous: set[int]
) -> tuple[dict[int, int], dict[str, list[int]]]:
    """
    Returns:
      representative: {first_occurrence_idx: count}  — deduplicated ambiguous lines
      groups: {normalized_key: [indices]}
    """
    groups: dict[str, list[int]] = {}
    for i in ambiguous:
        key = _normalize_line(lines[i])
        groups.setdefault(key, []).append(i)

    representative = {idxs[0]: len(idxs) for idxs in groups.values()}
    return representative, groups


# ── Stage 3: LLM refinement ───────────────────────────────────────────────────

async def _llm_call(prompt: str) -> str:
    text = await get_cloud_gateway().generate(
        prompt,
        task_type="log_filter",
        mode="economy",
        max_tokens=1200,
        temperature=0.0,
        timeout=90.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


async def _stage3_llm(
    lines: list[str],
    ambiguous_indices: list[int],
    focus: Optional[str],
) -> set[int]:
    """
    Ask qwen3 to classify ambiguous lines as KEEP or SKIP.
    Returns set of indices to KEEP.
    """
    keep: set[int] = set()

    focus_instruction = (
        f"\nFocus: only KEEP lines related to: {focus}\n"
        if focus else ""
    )

    for chunk_start in range(0, len(ambiguous_indices), CHUNK_SIZE):
        chunk = ambiguous_indices[chunk_start: chunk_start + CHUNK_SIZE]
        numbered = "\n".join(
            f"[{i}] {lines[i][:MAX_LINE_LEN]}"
            for i in chunk
        )

        prompt = f"""/no_think
You are a log filter for software debugging. Classify each numbered log line as KEEP or SKIP.

KEEP if: errors, warnings, exceptions, stack traces, connection failures, timeouts,
         HTTP 4xx/5xx, memory issues, crashes, authentication failures, anomalies.
SKIP if: routine info, debug messages, successful requests, heartbeats,
         cache operations, normal startup/shutdown messages.
{focus_instruction}
Return ONLY valid JSON array, no explanation:
[{{"i": <line_number>, "action": "KEEP"|"SKIP"}}]

Log lines:
{numbered}

JSON:"""

        try:
            raw = await _llm_call(prompt)
            raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            # Extract JSON array from response
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not match:
                # If LLM fails, keep all ambiguous lines (safe default)
                keep.update(chunk)
                continue
            decisions = json.loads(match.group())
            for d in decisions:
                if d.get("action") == "KEEP":
                    keep.add(int(d["i"]))
        except Exception as e:
            logger.warning("LLM chunk classification failed: %s — keeping all", e)
            keep.update(chunk)

    return keep


# ── Context expansion ──────────────────────────────────────────────────────────

def _expand_context(keep: set[int], total: int, context: int) -> set[int]:
    """Add N lines before and after each kept line."""
    expanded: set[int] = set()
    for i in keep:
        for j in range(max(0, i - context), min(total, i + context + 1)):
            expanded.add(j)
    return expanded


# ── Schemas ────────────────────────────────────────────────────────────────────

class FilterRequest(BaseModel):
    log_text: Optional[str] = Field(None, description="Raw log text")
    file_path: Optional[str] = Field(None, description="Absolute path to log file")
    focus: Optional[str] = Field(None, description="Focus topic, e.g. 'memory errors', 'auth failures'")
    context_lines: int = Field(CONTEXT_LINES, ge=0, le=20)
    max_output_lines: int = Field(1000, ge=10, le=10000)
    use_llm: bool = Field(True, description="Use LLM for ambiguous lines (slower but more accurate)")
    agent_id: str = Field("default")
    project_id: Optional[str] = Field(None, description="Project name for per-project learning")


class FilterResponse(BaseModel):
    original_lines: int
    after_regex: int
    after_dedup: int
    after_llm: int
    final_lines: int
    compression_ratio: float       # e.g. 0.02 = 98% reduction
    filtered_log: str              # the filtered output
    stats: dict


class FeedbackRequest(BaseModel):
    line: str                      # the line that was incorrectly kept
    project_id: Optional[str] = None
    agent_id: str = "default"


# ── Main endpoint ──────────────────────────────────────────────────────────────

@router.post("/filter", response_model=FilterResponse)
async def filter_log(body: FilterRequest, ollama: OllamaDep, qdrant: QdrantDep):
    """
    Filter a log file/text using local LLM.
    Removes noise, keeps errors/warnings/anomalies + surrounding context.
    """
    # Load input
    if body.file_path:
        p = Path(body.file_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {body.file_path}")
        raw = p.read_text(errors="replace")
    elif body.log_text:
        raw = body.log_text
    else:
        raise HTTPException(status_code=422, detail="Provide log_text or file_path")

    lines = raw.splitlines()
    if len(lines) > MAX_INPUT_LINES:
        lines = lines[-MAX_INPUT_LINES:]  # take last N lines (most recent)
        logger.info("Log truncated to last %d lines", MAX_INPUT_LINES)

    original_count = len(lines)

    # ── Stage 1: Regex ─────────────────────────────────────────────────────────
    keep_regex, skip_regex = _stage1_regex(lines, body.focus)
    ambiguous = set(range(len(lines))) - keep_regex - skip_regex
    after_regex = len(keep_regex) + len(ambiguous)  # before dedup

    # ── Stage 2: Dedup ambiguous lines ─────────────────────────────────────────
    representatives, groups = _stage2_dedup(lines, ambiguous)
    dedup_count_map: dict[int, int] = representatives  # idx → occurrence count
    ambiguous_deduped = sorted(representatives.keys())
    after_dedup = len(keep_regex) + len(ambiguous_deduped)

    # ── Stage 3: LLM on ambiguous ──────────────────────────────────────────────
    llm_keep: set[int] = set()
    if body.use_llm and ambiguous_deduped:
        llm_keep = await _stage3_llm(lines, ambiguous_deduped, body.focus)
    else:
        # Without LLM: keep all ambiguous (conservative)
        llm_keep = set(ambiguous_deduped)

    after_llm = len(keep_regex) + len(llm_keep)

    # ── Expand context around all kept lines ────────────────────────────────────
    all_keep = keep_regex | llm_keep
    all_keep_expanded = _expand_context(all_keep, len(lines), body.context_lines)

    # ── Build output ────────────────────────────────────────────────────────────
    output_lines: list[str] = []
    prev_i = -2
    for i in sorted(all_keep_expanded):
        if i - prev_i > 1 and prev_i >= 0:
            output_lines.append(f"... [{i - prev_i - 1} lines skipped] ...")
        line = lines[i]
        count = dedup_count_map.get(i, 1)
        if count > 1:
            output_lines.append(f"{line}  (×{count} occurrences)")
        else:
            output_lines.append(line)
        prev_i = i

    # Limit output
    if len(output_lines) > body.max_output_lines:
        output_lines = output_lines[:body.max_output_lines]
        output_lines.append(f"... [output truncated at {body.max_output_lines} lines] ...")

    final_count = len(output_lines)
    filtered_log = "\n".join(output_lines)
    compression = round(1 - final_count / max(original_count, 1), 4)

    return FilterResponse(
        original_lines=original_count,
        after_regex=after_regex,
        after_dedup=after_dedup,
        after_llm=after_llm,
        final_lines=final_count,
        compression_ratio=compression,
        filtered_log=filtered_log,
        stats={
            "kept_by_regex": len(keep_regex),
            "skipped_by_regex": len(skip_regex),
            "ambiguous_before_dedup": len(ambiguous),
            "ambiguous_after_dedup": len(ambiguous_deduped),
            "kept_by_llm": len(llm_keep),
        },
    )


@router.post("/feedback")
async def log_feedback(body: FeedbackRequest, ollama: OllamaDep, qdrant: QdrantDep):
    """
    Report a line that was incorrectly kept (false positive).
    Stores it as a noise pattern in Qdrant — improves future filtering.
    """
    try:
        from app.models.memory import MemoryCreate
        from app.models.enums import MemoryType
        normalized = _normalize_line(body.line)
        mem = MemoryCreate(
            content=f"[LOG-NOISE] {normalized}",
            agent_id=AGENT_ID,
            memory_type=MemoryType.fact,
            category="log-noise-pattern",
            importance_score=0.8,
            source="user-feedback",
            tags=["noise", body.project_id or "unknown"],
        )
        vector, embedding_meta = await embed_text(
            mem.content,
            primary=ollama,
            purpose="log_noise_feedback",
            fallback_reason="log_noise_feedback_embedding_unavailable",
        )
        mem.meta.update(embedding_meta)
        await qdrant.insert(mem, vector)
        return {"status": "ok", "stored": normalized}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
