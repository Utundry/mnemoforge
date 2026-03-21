"""
Context Assembly Layer — converts raw search results into model-ready context bundles.

Inspired by Zep context engineering and Mem0 memory layer UX:
  - Deduplicates overlapping memories (Jaccard similarity)
  - Groups by category
  - Formats into a compact, LLM-ready text block respecting a token budget

This transforms raw ranked SearchResults into a single context string
ready to inject directly into an LLM prompt — no client-side assembly needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.memory import SearchResult


@dataclass
class ContextBundle:
    context: str                  # ready-to-use text block for LLM prompt
    source_count: int             # total results before assembly
    used_count: int               # results after deduplication
    deduplicated_count: int       # how many were removed as near-duplicates
    categories: list[str]         # categories present in context
    tokens_estimate: int = field(default=0)  # rough token estimate (~4 chars/token)

    def __post_init__(self) -> None:
        self.tokens_estimate = len(self.context) // 4


class ContextService:
    """
    Assembles search results into a compact, deduplicated context bundle.

    Dedup strategy: Jaccard similarity on word sets.
    Results are sorted by score descending; later results are dropped if
    they share > DEDUP_THRESHOLD overlap with any already-kept result.
    """

    DEDUP_THRESHOLD = 0.60  # Jaccard similarity above this → treat as duplicate

    def assemble(
        self,
        results: list[SearchResult],
        max_tokens: int = 2000,
        fmt: str = "markdown",  # "text" | "markdown"
    ) -> ContextBundle:
        if not results:
            return ContextBundle(
                context="",
                source_count=0,
                used_count=0,
                deduplicated_count=0,
                categories=[],
            )

        # Step 1: deduplicate (highest-scored wins)
        kept = self._deduplicate(results)
        deduplicated_count = len(results) - len(kept)

        # Step 2: group by category
        groups: dict[str, list[SearchResult]] = {}
        for r in kept:
            groups.setdefault(r.memory.category, []).append(r)

        # Step 3: build context text respecting token budget
        chars_budget = max_tokens * 4
        lines: list[str] = []
        used: list[SearchResult] = []

        for category, items in sorted(groups.items()):
            if fmt == "markdown":
                lines.append(f"### {category.replace('-', ' ').title()}")

            for item in items:
                entry = self._format_entry(item, fmt)
                projected = len("\n".join(lines)) + len(entry) + 1
                if projected > chars_budget:
                    break
                lines.append(entry)
                used.append(item)

            if fmt == "markdown":
                lines.append("")  # blank line between groups

        context = "\n".join(lines).strip()

        return ContextBundle(
            context=context,
            source_count=len(results),
            used_count=len(used),
            deduplicated_count=deduplicated_count,
            categories=list(groups.keys()),
        )

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        kept: list[SearchResult] = []
        for r in sorted(results, key=lambda x: x.score, reverse=True):
            if not any(
                self._jaccard(r.memory.content, k.memory.content) > self.DEDUP_THRESHOLD
                for k in kept
            ):
                kept.append(r)
        return kept

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        a_words = set(re.sub(r"\W+", " ", a.lower()).split())
        b_words = set(re.sub(r"\W+", " ", b.lower()).split())
        if not a_words or not b_words:
            return 0.0
        return len(a_words & b_words) / len(a_words | b_words)

    @staticmethod
    def _format_entry(result: SearchResult, fmt: str) -> str:
        mem = result.memory
        score = round(result.score, 2)
        age_days = ""
        try:
            from datetime import datetime, timezone
            delta = datetime.now(timezone.utc) - mem.timestamp
            days = delta.days
            if days == 0:
                age_days = "today"
            elif days == 1:
                age_days = "1d ago"
            else:
                age_days = f"{days}d ago"
        except Exception:
            pass

        meta = f"score={score}"
        if age_days:
            meta += f", {age_days}"

        if fmt == "markdown":
            return f"- [{meta}] {mem.content}"
        return f"[{meta}] {mem.content}"


# Singleton — stateless, shared across all requests
_context_svc = ContextService()
