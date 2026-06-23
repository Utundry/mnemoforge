from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_DUPLICATE_THRESHOLD = 0.82
_MATCH_THRESHOLD = 0.46
_GAP_MATCH_THRESHOLD = 0.18
_STOPWORDS = {
    "a", "an", "and", "are", "as", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on",
    "or", "that", "the", "this", "to", "with", "must", "should", "need", "needs", "rule", "law",
    "project", "system", "agent", "agents", "task", "tasks", "improvement", "improvements",
}
_GAP_PATTERNS = (
    "not applied", "not apply", "not enforced", "not followed", "not comply", "does not comply",
    "violates", "violation", "fails to", "failed to", "missing enforcement", "retrieved but not applied",
    "ignored", "ignore", "gap", "defect", "bug",
)


@dataclass(frozen=True)
class AuthorityClassification:
    authority_layer: str
    reason: str
    matched_law_ref: str = ""
    matched_law_title: str = ""
    matched_law_status: str = ""
    matched_score: float = 0.0
    suppress_improvement: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "authority_layer": self.authority_layer,
            "classification_reason": self.reason,
            "matched_law_ref": self.matched_law_ref,
            "matched_law_title": self.matched_law_title,
            "matched_law_status": self.matched_law_status,
            "matched_law_score": round(self.matched_score, 3) if self.matched_score else None,
            "suppress_improvement": self.suppress_improvement,
        }


def classify_improvement_authority(*, project: str, title: str, summary: str, next_step: str = "", laws: list[dict[str, Any]] | None = None) -> AuthorityClassification:
    """Classify a proposed improvement without encoding project-specific law content."""
    text = " ".join(part for part in (title, summary, next_step) if part).strip()
    candidate_tokens = _tokens(text)
    if not candidate_tokens:
        return AuthorityClassification("product_capability", "No comparable proposal text was provided.")

    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for law in laws or []:
        if not isinstance(law, dict):
            continue
        law_tokens = _tokens(" ".join(str(law.get(key) or "") for key in ("title", "statement", "rationale")))
        if not law_tokens:
            continue
        score = _coverage(candidate_tokens, law_tokens)
        if score > best[0]:
            best = (score, law)

    score, law = best
    has_gap_signal = _has_gap_signal(text)
    if not law or score < (_GAP_MATCH_THRESHOLD if has_gap_signal else _MATCH_THRESHOLD):
        return AuthorityClassification("product_capability", "No active project law sufficiently matches the proposed improvement.")

    law_ref = _law_ref(project=project, law=law)
    law_title = str(law.get("title") or "").strip()
    law_status = str(law.get("status") or "").strip()
    if has_gap_signal:
        return AuthorityClassification(
            "application_gap",
            "The proposal references an active law but describes missing application or enforcement, so it remains actionable work.",
            matched_law_ref=law_ref,
            matched_law_title=law_title,
            matched_law_status=law_status,
            matched_score=score,
            suppress_improvement=False,
        )

    title_tokens = _tokens(title)
    law_title_tokens = _tokens(law_title)
    title_score = _coverage(title_tokens, law_title_tokens) if title_tokens and law_title_tokens else 0.0
    if score >= _DUPLICATE_THRESHOLD or title_score >= 0.9:
        return AuthorityClassification(
            "duplicate_law",
            "The proposal duplicates an active project law; reference the law instead of creating a product improvement.",
            matched_law_ref=law_ref,
            matched_law_title=law_title,
            matched_law_status=law_status,
            matched_score=max(score, title_score),
            suppress_improvement=True,
        )

    return AuthorityClassification(
        "project_law_related_product_capability",
        "The proposal is related to an active law but appears to request product capability beyond restating the law.",
        matched_law_ref=law_ref,
        matched_law_title=law_title,
        matched_law_status=law_status,
        matched_score=score,
        suppress_improvement=False,
    )


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text.casefold())
    return {token for token in raw if len(token) > 2 and token not in _STOPWORDS}


def _coverage(candidate: set[str], law: set[str]) -> float:
    if not candidate or not law:
        return 0.0
    overlap = len(candidate & law)
    return overlap / max(1, min(len(candidate), len(law)))


def _has_gap_signal(text: str) -> bool:
    folded = text.casefold()
    return any(pattern in folded for pattern in _GAP_PATTERNS)


def _law_ref(*, project: str, law: dict[str, Any]) -> str:
    law_id = str(law.get("id") or law.get("law_id") or "").strip()
    law_project = str(law.get("project") or project or "").strip()
    if law_id and law_project:
        return f"law:{law_project}:{law_id}"
    if law_id:
        return f"law:{law_id}"
    return ""
