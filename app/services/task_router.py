"""
Task Router — decides which component should handle a given task.

Routing priority:
  1. Cached skill  (instant, free)   — if score >= SKILL_THRESHOLD
  2. Local LLM     (fast, cheap)     — if score >= LOCAL_THRESHOLD
  3. Cloud LLM     (slow, expensive) — fallback

Decision is returned with reasoning, not executed here.
Caller records the outcome via Performance Tracker.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from app.services.capability_registry import get_registry
from app.services.llm_gateway import get_cloud_gateway

logger = logging.getLogger(__name__)

MANAGER_MODEL = "qwen3:1.7b"
SKILL_THRESHOLD = 0.80   # score above this → use cached skill
LOCAL_THRESHOLD = 0.65   # score above this → use local LLM
# Below LOCAL_THRESHOLD → escalate to cloud LLM
REFERENCE_THRESHOLD = 0.3  # if best score across ALL components is below this → out of domain

_CLASSIFY_PROMPT = """/no_think
Classify the following task into exactly ONE task type from the list below.
Return only the task type string, nothing else.

Task types:
  layout_fix          - fix keyboard layout errors (ru/en translit)
  log_filter          - filter/classify log lines, identify errors
  fact_extraction     - extract facts, memories from conversation text
  code_generation     - write new code, functions, classes
  code_review         - review existing code, find bugs, suggest improvements
  text_summarization  - summarize text, create short description
  skill_tagging       - assign domain tags to a skill or tool
  relevance_scoring   - score relevance between query and documents
  memory_extraction   - extract structured memories from JSONL conversations
  query_expansion     - expand or reformulate a search query
  architecture        - system design, architecture decisions, planning

Task: {task}

Return one of the task types above, exactly as written.
"""

_HEURISTIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("code_review", ("review", "bug", "regression", "risk", "audit", "inspect", "findings")),
    ("architecture", ("architecture", "design", "tradeoff", "roadmap", "strategy", "decompose", "benchmark")),
    ("code_generation", ("implement", "fix", "patch", "endpoint", "router", "mcp", "server", "api", "write scope", ".py", ".ts", ".js")),
    ("text_summarization", ("summarize", "summary", "short description", "overview")),
    ("fact_extraction", ("extract facts", "extract fact", "facts from", "memories from")),
    ("query_expansion", ("expand query", "reformulate query", "query expansion")),
    ("relevance_scoring", ("relevance", "rank documents", "score relevance")),
    ("memory_extraction", ("jsonl", "structured memories", "conversation memories")),
    ("skill_tagging", ("skill tags", "tag skill", "domain tags")),
    ("log_filter", ("log", "traceback", "stacktrace", "error lines")),
    ("layout_fix", ("keyboard layout", "translit", "ru/en")),
]


@dataclass
class RoutingDecision:
    task_type: str
    component: str
    score: float
    tier: str              # "skill" | "local" | "cloud" | "reference"
    reasoning: str
    alternatives: list     # [(component, score), ...]
    confidence: float      # how confident is the classification (0-1)
    cloud_fallbacks: list = field(default_factory=list)  # ranked fallback models if primary is cloud
    reference_url: Optional[str] = None   # populated when tier="reference"
    reference_endpoint: Optional[str] = None  # e.g. "112", "help@company.com"


async def _llm_classify(task: str) -> str:
    """Use the configured LLM gateway to classify task type."""
    text = await get_cloud_gateway().generate(
        _CLASSIFY_PROMPT.format(task=task[:500]),
        task_type="text_summarization",
        mode="economy",
        max_tokens=40,
        temperature=0.0,
        timeout=30.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text.lower().split()[0] if text else "text_summarization"


def _heuristic_classify(task: str) -> str | None:
    text = str(task or "").strip().lower()
    if not text:
        return None

    for task_type, markers in _HEURISTIC_RULES:
        if any(marker in text for marker in markers):
            return task_type

    return None


def _pick_cloud_model(task_type: str, cloud_entries: list) -> tuple[str, float, list]:
    """
    Use ModelRegistry to pick best available cloud model for task_type.
    Returns (component, score, fallbacks).
    Falls back to "cloud-llm" if ModelRegistry unavailable or all models exhausted.
    """
    try:
        from app.services.model_registry import get_model_registry
        ranked = get_model_registry().rank_for_task(task_type)
        if ranked:
            component, score = ranked[0]
            fallbacks = [{"model_id": m, "score": round(s, 3)} for m, s in ranked[1:4]]
            return component, score, fallbacks
    except Exception as e:
        logger.debug("ModelRegistry unavailable, using cloud-llm: %s", e)
    # Fallback to generic cloud-llm
    score = cloud_entries[0][1] if cloud_entries else 0.9
    return "cloud-llm", score, []


def _configured_local_components() -> set[str]:
    local_components = {
        MANAGER_MODEL,
        str(settings.learning_mirror_model or "").strip(),
        str(os.getenv("LOCAL_GENERATE_MODEL", "")).strip(),
        "qwen3:1.7b",
    }
    return {component for component in local_components if component}


def _configured_cloud_components(task_type: str) -> set[str]:
    try:
        from app.services.model_registry import get_model_registry

        return {model_id for model_id, _score in get_model_registry().rank_for_task(task_type)}
    except Exception as e:
        logger.debug("ModelRegistry unavailable while filtering cloud candidates: %s", e)
        return set()


def _filter_live_ranked_components(task_type: str, ranked: list[tuple[str, float]]) -> list[tuple[str, float]]:
    local_components = _configured_local_components()
    cloud_components = _configured_cloud_components(task_type)
    filtered: list[tuple[str, float]] = []

    for component, score in ranked:
        if component.startswith("skill:") or component == "cloud-llm":
            filtered.append((component, score))
            continue
        if component in local_components or component in cloud_components:
            filtered.append((component, score))

    return filtered


def _route(task_type: str, preferred_tier: Optional[str] = None) -> RoutingDecision:
    """Apply routing policy to select best component for a task type."""
    reg = get_registry()
    ranked = _filter_live_ranked_components(task_type, reg.best_for(task_type))

    if not ranked:
        return RoutingDecision(
            task_type=task_type,
            component="reference",
            score=0.0,
            tier="reference",
            reasoning="No capability data for this task type — check pinned references or escalate manually",
            alternatives=[],
            confidence=0.0,
        )

    best_component, best_score = ranked[0]
    alternatives = ranked[1:]

    # Find best skill if any
    skill_entries = [(c, s) for c, s in ranked if c.startswith("skill:")]
    local_entries = [(c, s) for c, s in ranked if not c.startswith("skill:") and c != "cloud-llm"]
    cloud_entries = [(c, s) for c, s in ranked if c == "cloud-llm"]

    # Out-of-domain detection: if best score is near zero → no component has real capability data
    if preferred_tier != "cloud" and preferred_tier != "local" and best_score < REFERENCE_THRESHOLD:
        return RoutingDecision(
            task_type=task_type,
            component="reference",
            score=best_score,
            tier="reference",
            reasoning=(
                f"Best component score {best_score:.2f} < {REFERENCE_THRESHOLD} threshold — "
                f"task appears out of domain. Check pinned references."
            ),
            alternatives=alternatives[:3],
            confidence=0.0,
        )

    # Routing policy
    cloud_fallbacks: list = []
    if preferred_tier == "cloud":
        component, score, cloud_fallbacks = _pick_cloud_model(task_type, cloud_entries)
        tier = "cloud"
        reasoning = f"Forced cloud tier. Best local was {local_entries[0][0]} ({local_entries[0][1]:.2f}) if available." if local_entries else "Forced cloud tier."
    elif skill_entries and skill_entries[0][1] >= SKILL_THRESHOLD and preferred_tier != "local":
        component, score = skill_entries[0]
        tier = "skill"
        reasoning = f"Cached skill score {score:.2f} ≥ {SKILL_THRESHOLD} threshold — instant execution."
    elif local_entries and local_entries[0][1] >= LOCAL_THRESHOLD and preferred_tier != "cloud":
        component, score = local_entries[0]
        tier = "local"
        reasoning = f"Local LLM score {score:.2f} ≥ {LOCAL_THRESHOLD} threshold — using local."
    else:
        component, score, cloud_fallbacks = _pick_cloud_model(task_type, cloud_entries)
        tier = "cloud"
        local_score = local_entries[0][1] if local_entries else 0.0
        reasoning = f"Local LLM score {local_score:.2f} < {LOCAL_THRESHOLD} threshold — escalating to cloud."

    return RoutingDecision(
        task_type=task_type,
        component=component,
        score=score,
        tier=tier,
        reasoning=reasoning,
        alternatives=alternatives[:3],
        confidence=min(1.0, score * 1.2),
        cloud_fallbacks=cloud_fallbacks,
    )


async def decide(
    task: str,
    task_type: Optional[str] = None,
    preferred_tier: Optional[str] = None,
) -> RoutingDecision:
    """Classify task and return routing decision."""
    if not task_type:
        heuristic_type = _heuristic_classify(task)
        if heuristic_type:
            task_type = heuristic_type
        else:
            try:
                task_type = await _llm_classify(task)
                # Validate
                known = get_registry().task_types()
                if task_type not in known:
                    task_type = "text_summarization"  # safe fallback
            except Exception as e:
                fallback_type = _heuristic_classify(task) or "text_summarization"
                logger.warning("Task classification failed; fallback=%s error=%s", fallback_type, e)
                task_type = fallback_type

    return _route(task_type, preferred_tier=preferred_tier)
