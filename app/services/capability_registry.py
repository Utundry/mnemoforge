"""
Capability Registry — tracks what each component can do and how well.

Storage: JSON file (qdrant_data/capabilities.json) + in-memory cache.
Scores are Bayesian-updated: success/fail counts → Wilson score confidence interval.

Components:
  - qwen3:1.7b        — local LLM, fast/cheap
  - claude-*          — cloud LLM, slow/expensive
  - skill:<name>      — cached skill, instant/free
  - qdrant            — vector search

Task types:
  layout_fix, log_filter, fact_extraction, code_generation,
  code_review, text_summarization, skill_tagging, relevance_scoring,
  memory_extraction, query_expansion
"""

import json
import logging
import math
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default capability scores (seed values before real data accumulates)
_DEFAULTS: dict[str, dict[str, dict]] = {
    "qwen3:1.7b": {
        "layout_fix":        {"success": 40, "fail": 2,  "description": "Rule+LLM layout correction"},
        "log_filter":        {"success": 30, "fail": 3,  "description": "LLM log classification"},
        "fact_extraction":   {"success": 25, "fail": 5,  "description": "Extract facts from conversation"},
        "skill_tagging":     {"success": 20, "fail": 2,  "description": "Auto-tag skills with domain"},
        "relevance_scoring": {"success": 15, "fail": 3,  "description": "Score skill relevance to context"},
        "text_summarization":{"success": 20, "fail": 4,  "description": "Summarize text chunks"},
        "query_expansion":   {"success": 18, "fail": 3,  "description": "Expand search query"},
        "code_generation":   {"success": 3,  "fail": 10, "description": "Write new code"},
        "code_review":       {"success": 5,  "fail": 8,  "description": "Review and critique code"},
        "memory_extraction": {"success": 22, "fail": 4,  "description": "Extract memories from JSONL"},
    },
    "cloud-llm": {
        "layout_fix":        {"success": 12, "fail": 0,  "description": "Cloud layout correction"},
        "log_filter":        {"success": 10, "fail": 0,  "description": "Cloud log classification"},
        "fact_extraction":   {"success": 30, "fail": 1,  "description": "Cloud fact extraction"},
        "skill_tagging":     {"success": 15, "fail": 0,  "description": "Cloud skill tagging"},
        "relevance_scoring": {"success": 20, "fail": 0,  "description": "Cloud relevance scoring"},
        "text_summarization":{"success": 25, "fail": 0,  "description": "Cloud summarization"},
        "query_expansion":   {"success": 20, "fail": 0,  "description": "Cloud query expansion"},
        "code_generation":   {"success": 80, "fail": 4,  "description": "Cloud code generation"},
        "code_review":       {"success": 60, "fail": 3,  "description": "Cloud code review"},
        "memory_extraction": {"success": 28, "fail": 1,  "description": "Cloud memory extraction"},
        "architecture":      {"success": 50, "fail": 2,  "description": "System design and architecture"},
    },
}

_TASK_TYPES = sorted({t for comp in _DEFAULTS.values() for t in comp})


def _wilson_score(success: int, fail: int, z: float = 1.28) -> float:
    """Wilson score lower bound — conservative estimate of true success rate."""
    n = success + fail
    if n == 0:
        return 0.0
    p = success / n
    return (p + z*z/(2*n) - z * math.sqrt((p*(1-p) + z*z/(4*n))/n)) / (1 + z*z/n)


class CapabilityRegistry:
    def __init__(self, storage_path: Path):
        self._path = storage_path
        self._data: dict[str, dict[str, dict]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                logger.info("Capability registry loaded: %d components", len(self._data))
                return
            except Exception as e:
                logger.warning("Failed to load capability registry: %s", e)
        # Seed with defaults
        self._data = {k: {t: dict(v) for t, v in caps.items()} for k, caps in _DEFAULTS.items()}
        self._save()
        logger.info("Capability registry seeded with defaults")

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error("Failed to save capability registry: %s", e)

    def score(self, component: str, task_type: str) -> float:
        """Return Wilson score for component+task. 0.0 if unknown."""
        caps = self._data.get(component, {})
        entry = caps.get(task_type)
        if not entry:
            return 0.0
        return _wilson_score(entry.get("success", 0), entry.get("fail", 0))

    def best_for(self, task_type: str, exclude: Optional[list[str]] = None) -> list[tuple[str, float]]:
        """Return components sorted by score for a task type (best first)."""
        exclude = exclude or []
        results = []
        for component, caps in self._data.items():
            if component in exclude:
                continue
            if task_type in caps:
                s = self.score(component, task_type)
                results.append((component, s))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def update(self, component: str, task_type: str, success: bool, description: str = "") -> float:
        """Record outcome and return new score."""
        if component not in self._data:
            self._data[component] = {}
        if task_type not in self._data[component]:
            self._data[component][task_type] = {"success": 0, "fail": 0, "description": description}
        entry = self._data[component][task_type]
        if success:
            entry["success"] = entry.get("success", 0) + 1
        else:
            entry["fail"] = entry.get("fail", 0) + 1
        if description:
            entry["description"] = description
        self._save()
        return self.score(component, task_type)

    def register(self, component: str, task_type: str, initial_score: float = 0.5, description: str = "") -> None:
        """Register a new component capability with initial score."""
        if component not in self._data:
            self._data[component] = {}
        if task_type not in self._data[component]:
            # Convert initial_score to synthetic success/fail counts (n=10)
            s = max(0, min(10, round(initial_score * 10)))
            self._data[component][task_type] = {"success": s, "fail": 10 - s, "description": description}
            self._save()

    def components(self) -> dict[str, dict[str, dict]]:
        """Return full registry with computed scores."""
        result = {}
        for comp, caps in self._data.items():
            result[comp] = {}
            for task, entry in caps.items():
                result[comp][task] = {
                    "score": round(self.score(comp, task), 3),
                    "success": entry.get("success", 0),
                    "fail": entry.get("fail", 0),
                    "description": entry.get("description", ""),
                }
        return result

    def task_types(self) -> list[str]:
        return sorted({t for caps in self._data.values() for t in caps})


# Singleton
_registry: Optional[CapabilityRegistry] = None


def get_registry() -> CapabilityRegistry:
    global _registry
    if _registry is None:
        from app.config import settings
        path = Path("qdrant_data") / "capabilities.json"
        _registry = CapabilityRegistry(path)
    return _registry
