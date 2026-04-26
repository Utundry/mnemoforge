from __future__ import annotations

import re
from typing import Optional

from app.services.text_localization import is_low_quality_text, normalize_text_for_display

_SKILL_GAP_PREFIX_RE = re.compile(r"^\s*skill gap detected\s*:\s*(.+?)\s*$", re.IGNORECASE)

_GENERIC_SINGLETONS = frozenset({
    "configuration",
    "guidance",
    "maintenance",
    "management",
    "operational",
    "operations",
    "optimization",
    "procedure",
    "procedures",
    "recovery",
    "setup",
    "support",
    "workflow",
})

_GENERIC_TWO_TOKEN_PHRASES = frozenset({
    ("operational", "guidance"),
    ("operational", "skill"),
    ("operational", "skills"),
    ("procedural", "guidance"),
    ("service", "setup"),
    ("system", "optimization"),
})

_GENERIC_HEADS = frozenset({
    "administration",
    "configuration",
    "maintenance",
    "management",
    "operations",
    "optimization",
    "setup",
    "support",
})

_BROAD_SCOPE_MODIFIERS = frozenset({
    "database",
    "disk",
    "memory",
    "service",
    "system",
})

_GENERIC_ALIASES = {
    "api config": "api configuration",
    "api configuration": "api configuration",
    "api setup": "api configuration",
    "api_config": "api configuration",
    "diagnostics": "troubleshooting",
    "troubleshooting": "troubleshooting",
}


def _normalize_domain_key(value: str) -> str:
    cleaned = normalize_text_for_display(value or "")
    cleaned = re.sub(r"_+", " ", cleaned.casefold())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" '\"")
    return cleaned


def canonicalize_skill_gap_domain(value: str) -> Optional[str]:
    alias_key = _normalize_domain_key(value)
    if not alias_key or is_low_quality_text(alias_key):
        return None

    canonical = _GENERIC_ALIASES.get(alias_key, alias_key)
    tokens = canonical.split()
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0] in _GENERIC_SINGLETONS:
        return None
    if len(tokens) == 2 and tuple(tokens) in _GENERIC_TWO_TOKEN_PHRASES:
        return None
    if len(tokens) == 2 and tokens[0] in _BROAD_SCOPE_MODIFIERS and tokens[1] in _GENERIC_HEADS:
        return None
    return canonical


def canonicalize_skill_gap_title(title: str) -> Optional[str]:
    cleaned = normalize_text_for_display(title or "")
    match = _SKILL_GAP_PREFIX_RE.match(cleaned)
    if not match:
        return None
    domain = canonicalize_skill_gap_domain(match.group(1))
    if not domain:
        return None
    return f"Skill gap detected: {domain}"


def infer_skill_gap_domains_from_transcript(transcript: str) -> list[str]:
    return []


def refine_skill_gap_domains(domains: list[str], transcript: str) -> list[str]:
    del transcript
    refined: list[str] = []
    seen: set[str] = set()
    for raw_domain in domains:
        domain = canonicalize_skill_gap_domain(raw_domain)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        refined.append(domain)
    return refined
