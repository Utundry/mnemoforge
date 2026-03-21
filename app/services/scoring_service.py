from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models.memory import MemoryRecord, SearchResult


class ContextHint:
    """Caller-supplied metadata used to boost retrieval score."""
    __slots__ = ("project", "file_path", "task_type")

    def __init__(
        self,
        project: Optional[str] = None,
        file_path: Optional[str] = None,
        task_type: Optional[str] = None,
    ):
        self.project = project
        self.file_path = file_path
        self.task_type = task_type

    def is_empty(self) -> bool:
        return not (self.project or self.file_path or self.task_type)


class ScoringService:
    """
    composite_score = 0.6 * cosine_similarity
                    + 0.2 * importance_score
                    + 0.2 * recency_boost
                    + context_boost  (0..0.15, optional)

    recency_boost = 1 / (days_old * decay_rate + 1)

    context_boost:  memory tags matching project:/file:/task_type: hints each add +0.05
    decay_rate controls staleness speed:
      0.0  → never decays (permanent facts: math, history)
      1.0  → standard decay (default)
      3.0+ → fast decay (news, prices, statuses)
    """

    SIM_WEIGHT = 0.6
    IMP_WEIGHT = 0.2
    REC_WEIGHT = 0.2
    CTX_BOOST_PER_MATCH = 0.05  # per matching context field

    def score(self, record: MemoryRecord, similarity: float, ctx: Optional[ContextHint] = None) -> float:
        now = datetime.now(timezone.utc)
        days_old = (now - record.timestamp).total_seconds() / 86400
        recency = 1.0 / (days_old * record.decay_rate + 1.0)
        base = (
            self.SIM_WEIGHT * similarity
            + self.IMP_WEIGHT * record.importance_score
            + self.REC_WEIGHT * recency
        )
        if ctx and not ctx.is_empty():
            base += self._context_boost(record, ctx)
        # Hard expiry: expired content is nearly invisible in ranking
        if record.expires_at and record.expires_at < now:
            base *= 0.02
        return base

    def _context_boost(self, record: MemoryRecord, ctx: ContextHint) -> float:
        """Boost score when memory tags match caller context (project/file/task_type)."""
        boost = 0.0
        tags = set(record.tags)
        cat = record.category or ""
        # Match project
        if ctx.project:
            proj_tag = f"project:{ctx.project}"
            if proj_tag in tags or cat == ctx.project:
                boost += self.CTX_BOOST_PER_MATCH
        # Match file path (check prefix match — file may be a subpath)
        if ctx.file_path:
            for t in tags:
                if t.startswith("file:") and (ctx.file_path in t or t[5:] in ctx.file_path):
                    boost += self.CTX_BOOST_PER_MATCH
                    break
        # Match task type
        if ctx.task_type:
            task_tag = f"task_type:{ctx.task_type}"
            if task_tag in tags or f"task:{ctx.task_type}" in tags:
                boost += self.CTX_BOOST_PER_MATCH
        return boost

    def rank(
        self,
        raw_results: list[tuple[MemoryRecord, float]],
        limit: int,
        min_score: float = 0.0,
        ctx: Optional[ContextHint] = None,
    ) -> list[SearchResult]:
        scored = [
            SearchResult(
                memory=record,
                score=self.score(record, sim, ctx),
                similarity=sim,
            )
            for record, sim in raw_results
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return [r for r in scored[:limit] if r.score >= min_score]
