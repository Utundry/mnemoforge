from __future__ import annotations

import re
from typing import Optional

from app.services.text_localization import (
    is_low_quality_text,
    normalize_text_for_display,
)

_PROJECT_CORE_TERMS = frozenset({"supermemory"})

_PROCEDURAL_TERM_MARKERS = frozenset({
    "check",
    "checks",
    "cleanup",
    "configuration",
    "configure",
    "deployment",
    "guide",
    "guidance",
    "installation",
    "integrity",
    "maintenance",
    "migration",
    "procedure",
    "procedures",
    "recovery",
    "repair",
    "restore",
    "rollback",
    "runbook",
    "setup",
    "step",
    "steps",
    "troubleshooting",
    "upgrade",
    "verification",
    "verify",
    "workflow",
})

_PROCEDURAL_CONTEXT_MARKERS = frozenset({
    "commands",
    "expected outputs",
    "how do i",
    "how to",
    "lacked guidance",
    "missing operational skill",
    "need guidance",
    "no guidance",
    "not new terminology",
    "operational guidance",
    "reusable procedure",
    "rollback notes",
    "runbook",
    "verification steps",
    "we do not have validated guidance",
})

_SUCCESS_CONTEXT_MARKERS = frozenset({
    "helped",
    "resolved",
    "solved",
    "successful",
    "this worked",
    "use this again",
    "useful",
    "worked well",
})

_ABSTRACT_PATTERN_TOKENS = frozenset({
    "acknowledging",
    "analysis",
    "capturing",
    "careful",
    "collaboration",
    "communication",
    "comprehensive",
    "context",
    "documentation",
    "gaps",
    "knowledge",
    "thorough",
    "understanding",
})


def _normalize_phrase(value: str) -> str:
    cleaned = normalize_text_for_display(value or "")
    cleaned = re.sub(r"[-_]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" '\"")
    return cleaned


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def transcript_prefers_procedural_interpretation(transcript: str) -> bool:
    lowered = _normalize_phrase(transcript).casefold()
    if not lowered:
        return False
    return any(marker in lowered for marker in _PROCEDURAL_CONTEXT_MARKERS)


def looks_like_procedural_phrase(value: str) -> bool:
    tokens = _tokenize(value)
    if not tokens:
        return False
    if any(token in _PROCEDURAL_TERM_MARKERS for token in tokens):
        return True
    return len(tokens) >= 2 and any(token.endswith("ing") for token in tokens)


def should_reclassify_as_missing_skill(value: str, transcript: str = "") -> bool:
    cleaned = _normalize_phrase(value)
    if not cleaned or is_low_quality_text(cleaned):
        return False
    return looks_like_procedural_phrase(cleaned) and transcript_prefers_procedural_interpretation(transcript)


def sanitize_new_terminology_candidate(value: str, transcript: str = "") -> Optional[str]:
    cleaned = _normalize_phrase(value)[:60]
    if not cleaned or is_low_quality_text(cleaned):
        return None

    lowered = cleaned.casefold()
    if lowered in _PROJECT_CORE_TERMS:
        return None
    if should_reclassify_as_missing_skill(cleaned, transcript=transcript):
        return None
    return cleaned


def sanitize_successful_pattern_candidate(value: str, transcript: str = "") -> Optional[str]:
    cleaned = _normalize_phrase(value)[:60]
    if not cleaned or is_low_quality_text(cleaned):
        return None

    tokens = _tokenize(cleaned)
    if not tokens:
        return None
    if all(token in _ABSTRACT_PATTERN_TOKENS for token in tokens):
        return None
    return cleaned
