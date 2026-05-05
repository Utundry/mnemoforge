"""
External Best-Practice Scout.

Implements:
  check_sufficiency() — sufficiency gate: are there enough active rules for this context?
  fetch_best_practices() — fetch 2-3 best practices from LLM, parse into structured BestPractice objects.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from app.services.llm_gateway import get_cloud_gateway

logger = logging.getLogger(__name__)

_SUFFICIENCY_MIN_ARTIFACTS = int(os.getenv("SCOUT_MIN_ARTIFACTS", "3"))


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SufficiencyResult:
    sufficient: bool
    active_count: int
    missing_skill_count: int
    missing_domains: list[str]
    reason: str
    confidence: float


@dataclass
class BestPractice:
    title: str
    domain: str
    what_and_why: str
    pros: str
    cons: str
    when_not_to_use: str
    example: str
    expected_result: str


# ── Sufficiency gate ───────────────────────────────────────────────────────────

async def check_sufficiency(
    store,
    project: str,
    task: str,
    agent_id: str = "",
    context_signature: str = "",
    since_hours: float = 168.0,
) -> SufficiencyResult:
    """
    Check if current knowledge is sufficient for the given task/project.

    Criteria for insufficient:
    - fewer than SCOUT_MIN_ARTIFACTS active artifacts for this project/context, OR
    - missing_skill signals detected in recent events.
    """
    active_count = await store.count_active_artifacts(
        project=project,
        context_signature=context_signature,
    )

    since_ts = time.time() - since_hours * 3600
    events = await store.list_events(
        agent_id=agent_id or None,
        since_ts=since_ts,
        limit=200,
    )

    missing_domains: list[str] = []
    missing_skill_count = 0
    for ev in events:
        payload_raw = ev.get("payload_json") or ev.get("payload") or "{}"
        if isinstance(payload_raw, str):
            try:
                payload = json.loads(payload_raw)
            except Exception:
                payload = {}
        else:
            payload = payload_raw or {}

        # Detect missing_skill signals in various payload shapes
        skill = (
            payload.get("missing_skill")
            or payload.get("skill_gap")
            or (payload.get("skill") if payload.get("gap") else None)
        )
        if skill:
            missing_skill_count += 1
            domain = str(skill)
            if domain and domain not in missing_domains:
                missing_domains.append(domain)

    sufficient = active_count >= _SUFFICIENCY_MIN_ARTIFACTS and missing_skill_count == 0

    if not sufficient:
        parts = []
        if active_count < _SUFFICIENCY_MIN_ARTIFACTS:
            parts.append(f"only {active_count}/{_SUFFICIENCY_MIN_ARTIFACTS} active rules")
        if missing_skill_count:
            parts.append(f"{missing_skill_count} skill-gap signal(s): {', '.join(missing_domains)}")
        reason = "; ".join(parts)
        confidence = min(0.4 + missing_skill_count * 0.1, 0.85)
    else:
        reason = f"{active_count} active rules, no skill gaps"
        confidence = 0.8

    return SufficiencyResult(
        sufficient=sufficient,
        active_count=active_count,
        missing_skill_count=missing_skill_count,
        missing_domains=missing_domains,
        reason=reason,
        confidence=confidence,
    )


# ── LLM fetch ─────────────────────────────────────────────────────────────────

_FETCH_PROMPT = """\
You are a best-practice advisor for software engineering teams.

Task: {task}
Project: {project}
Missing skill areas: {domains}

Provide exactly 3 best practices most relevant to the task and missing skills.
For each, use this EXACT format (no extra sections):

---PRACTICE---
TITLE: <short descriptive title>
DOMAIN: <one word: git|docker|ci|testing|security|python|linux|etc>
WHAT_AND_WHY: <what this practice is and why it matters, 1-2 sentences>
PROS: <2-3 bullet points>
CONS: <1-2 bullet points>
WHEN_NOT_TO_USE: <one sentence about when to skip>
EXAMPLE: <concrete command or code snippet>
EXPECTED_RESULT: <what the user should see/get after running the example, 1 sentence>
---END---

Be concise. Do not add explanations outside the ---PRACTICE--- blocks."""


def _field_value(block: str, name: str) -> str:
    """Extract a field value from a practice block."""
    m = re.search(
        rf"^{name}:\s*(.+?)(?=\n[A-Z_]{{2,}}:|$)",
        block,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _parse_practices(raw: str) -> list[BestPractice]:
    """Parse LLM output into BestPractice objects. Returns at most 3."""
    practices: list[BestPractice] = []
    blocks = re.split(r"---PRACTICE---", raw, flags=re.IGNORECASE)
    for block in blocks[1:]:
        end = re.search(r"---END---", block, re.IGNORECASE)
        if end:
            block = block[: end.start()]
        title = _field_value(block, "TITLE")
        if not title:
            continue
        practices.append(BestPractice(
            title=title,
            domain=_field_value(block, "DOMAIN").lower(),
            what_and_why=_field_value(block, "WHAT_AND_WHY"),
            pros=_field_value(block, "PROS"),
            cons=_field_value(block, "CONS"),
            when_not_to_use=_field_value(block, "WHEN_NOT_TO_USE"),
            example=_field_value(block, "EXAMPLE"),
            expected_result=_field_value(block, "EXPECTED_RESULT"),
        ))
    return practices[:3]


async def fetch_best_practices(
    ollama,
    project: str,
    task: str,
    domains: list[str],
) -> list[BestPractice]:
    """Ask LLM for 2-3 best practices. Returns empty list on failure."""
    prompt = _FETCH_PROMPT.format(
        task=task,
        project=project or "unknown",
        domains=", ".join(domains) if domains else "general software engineering",
    )
    raw = await get_cloud_gateway().generate(
        prompt,
        task_type="text_summarization",
        mode="economy",
        max_tokens=900,
        temperature=0.2,
        timeout=120.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    if not raw:
        logger.warning("best_practice_scout: LLM returned empty response")
        return []
    practices = _parse_practices(raw)
    logger.info("best_practice_scout: parsed %d practice(s) from LLM", len(practices))
    return practices
