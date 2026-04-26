from __future__ import annotations

import re
from typing import Any

from app.services.text_localization import normalize_text_for_display


_LABELED_PREFIXES = {
    "assumption": {"assumption", "assumptions"},
    "constraint": {"constraint", "constraints"},
    "definition_of_done": {"definition_of_done", "definition of done", "done", "dod"},
    "blocker": {"blocker", "blockers"},
    "open_question": {"open_question", "open question", "question", "questions"},
    "verification": {"verification", "validated", "tested", "test", "checks"},
    "remaining_risk": {"remaining_risk", "remaining risk", "risk", "risks"},
    "objective": {"objective", "goal", "scope"},
}
_FILE_REF_PATTERN = re.compile(r"(?:app|tests|docs|scripts|mcp|static|cli)/[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+(?::\d+(?:-\d+)?)?")


def _clean(value: Any, limit: int = 1000) -> str:
    return normalize_text_for_display(str(value or ""))[:limit].strip()


def looks_like_verification_evidence(value: Any) -> bool:
    text = _clean(value, limit=1000)
    if not text:
        return False
    scrubbed = _FILE_REF_PATTERN.sub(" ", text).casefold()
    negative_patterns = (
        r"\bno\s+(?:explicit\s+)?(?:verification|tests?|validation|checks?)\b",
        r"\bwithout\s+(?:explicit\s+)?(?:verification|tests?|validation|checks?)\b",
        r"\bmissing\s+(?:verification|tests?|validation|checks?)\b",
        r"\bneeds?\s+(?:more\s+|additional\s+)?(?:verification|tests?|validation|checks?)\b",
        r"\bnot\s+(?:verified|tested|validated|checked)\b",
        r"\bverification\s+was\s+not\b",
        r"\bno\s+verification\s+was\s+captured\b",
    )
    if any(re.search(pattern, scrubbed) for pattern in negative_patterns):
        return False
    return bool(re.search(r"\b(verify|verified|verification|test|tested|testing|validated|validation|checked|checks)\b", scrubbed))


def _has_task_checkpoint(changes: list[Any]) -> bool:
    for change in changes:
        content = _clean(getattr(change, "content", ""), limit=1000).casefold()
        tags = {
            str(tag).strip().casefold()
            for tag in (getattr(change, "tags", None) or [])
            if str(tag).strip()
        }
        if "task_checkpoint" in tags or "[task_checkpoint]" in content:
            return True
    return False


def collect_labeled_task_statements(*texts: str) -> dict[str, list[str]]:
    result = {key: [] for key in _LABELED_PREFIXES}
    for raw in texts:
        for line in str(raw or "").splitlines():
            text = _clean(line, limit=600)
            if not text:
                continue
            text = re.sub(r"^\s*\[[A-Za-z_]+\]\s*", "", text)
            match = re.match(r"^\s*[-*]?\s*([A-Za-z_ ]+)\s*:\s*(.+?)\s*$", text)
            if not match:
                continue
            label = match.group(1).strip().lower()
            value = _clean(match.group(2), limit=400)
            if not value:
                continue
            for canonical, aliases in _LABELED_PREFIXES.items():
                if label in aliases and value not in result[canonical]:
                    result[canonical].append(value)
    return result


def compute_task_statement_missing_artifacts(
    *,
    title: str,
    description: str,
    status: str,
    changes: list[Any],
) -> list[str]:
    normalized_title = _clean(title, limit=256)
    normalized_description = _clean(description, limit=2000)
    texts = [normalized_description]
    verification_hints: list[str] = []
    execution_signal = False

    for change in changes:
        content = _clean(getattr(change, "content", ""), limit=1000)
        why = _clean(getattr(change, "why", ""), limit=600)
        change_type = str(getattr(change, "change_type", "") or "").strip()
        if content:
            texts.append(content)
            if looks_like_verification_evidence(content):
                verification_hints.append(content)
        if why:
            texts.append(why)
        if change_type in {"implementation", "status_change", "decision"}:
            execution_signal = True

    labeled = collect_labeled_task_statements(*texts)
    missing: list[str] = []

    if not normalized_title and not normalized_description:
        missing.append("task_description")

    if not labeled["definition_of_done"]:
        missing.append("definition_of_done")

    if status in {"active", "paused", "done"} and not execution_signal:
        missing.append("task_changes")

    if status == "done":
        has_verification = bool(labeled["verification"] or verification_hints)
        has_remaining_risk = bool(labeled["remaining_risk"])
        if not has_verification and not has_remaining_risk:
            missing.append("verification_result")

    if status in {"active", "paused", "done"} and not _has_task_checkpoint(changes):
        missing.append("task_checkpoint")

    seen: set[str] = set()
    ordered: list[str] = []
    for item in missing:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def compute_task_statement_incomplete(
    *,
    title: str,
    description: str,
    status: str,
    changes: list[Any],
    pending_capture_count: int = 0,
) -> bool:
    if pending_capture_count > 0:
        return True
    return bool(
        compute_task_statement_missing_artifacts(
            title=title,
            description=description,
            status=status,
            changes=changes,
        )
    )
