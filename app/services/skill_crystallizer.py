"""
Skill Crystallizer — converts successful cloud LLM solutions into reusable skills.

Pipeline:
  1. Receive: task description + cloud solution
  2. qwen3:1.7b assesses reusability (0-1 score)
  3. If score >= CRYSTALLIZE_THRESHOLD → generate SKILL.md
  4. Publish to skill marketplace with auto domain tags
  5. Register skill in Capability Registry
  6. Next invocation: routed to skill tier (instant/free)
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MANAGER_MODEL = "qwen3:1.7b"
CRYSTALLIZE_THRESHOLD = 0.65  # min reusability score to create a skill

_ASSESS_PROMPT = """/no_think
Assess whether this cloud LLM solution is worth packaging as a reusable skill.

A solution is worth crystallizing if:
- The same type of task will likely appear again (not a one-off)
- The solution has a clear, repeatable procedure
- The procedure doesn't require deep reasoning — it's mostly a recipe
- It would save meaningful cloud tokens if handled locally

Return JSON with:
- "reusable": float 0.0-1.0 (1.0 = definitely reusable)
- "skill_name": slug name (lowercase-hyphen, e.g. "deploy-cloudflare")
- "reason": one sentence why or why not

Task: {task}
Solution preview: {solution_preview}

Return only valid JSON.
"""

_GENERATE_SKILL_PROMPT = """/no_think
Generate a SKILL.md file for a reusable skill based on this solved task.

The SKILL.md must follow this exact structure:
```
# {skill_name_title}

One-line description of what this skill does.

## When to use

- Bullet list of trigger conditions (when should an LLM invoke this skill?)

## Instructions

Step-by-step instructions for executing this skill.
Be precise and actionable. Reference specific tools, APIs, or commands where relevant.

## Examples

Show 2-3 concrete examples of inputs and expected outputs.
```

Task that was solved: {task}

Solution that worked:
{solution}

Generate the complete SKILL.md content. Start directly with "# " heading.
"""


async def _llm(prompt: str, timeout: float = 60.0) -> str:
    """Call cloud LLM if configured, otherwise fall back to local Ollama."""
    from app.services.cloud_llm import cloud_available, cloud_provider
    from app.services.llm_gateway import get_cloud_gateway
    from app.services.performance_tracker import get_tracker
    from time import perf_counter

    if cloud_available():
        started = perf_counter()
        try:
            result = await get_cloud_gateway().generate(
                prompt,
                task_type="text_summarization",
                mode="economy",
                max_tokens=700,
                temperature=0.2,
                timeout=timeout,
                allow_local_fallback=True,
                prefer_local=True,
            )
            get_tracker().record(
                component=cloud_provider(), task_type="crystallize_llm",
                success=True, latency_ms=round((perf_counter() - started) * 1000, 1),
            )
            return result
        except Exception as e:
            get_tracker().record(
                component=cloud_provider(), task_type="crystallize_llm",
                success=False, latency_ms=round((perf_counter() - started) * 1000, 1),
            )
            logger.warning("Cloud LLM failed, falling back to local: %s", e)

    # Local fallback — Ollama
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            f"{settings.ollama_base_url}/api/generate",
            json={"model": MANAGER_MODEL, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        text = r.json()["response"].strip()
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


@dataclass
class CrystallizationResult:
    crystallized: bool
    skill_id: Optional[str]
    skill_name: Optional[str]
    reusability_score: float
    reason: str
    skill_content: Optional[str] = None


async def assess(task: str, solution: str) -> tuple[float, str, str]:
    """
    Assess reusability of a solution.
    Returns (score, skill_name, reason).
    """
    prompt = _ASSESS_PROMPT.format(
        task=task[:300],
        solution_preview=solution[:500],
    )
    raw = await _llm(prompt)
    match = re.search(r'\{[^}]+\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            score = float(data.get("reusable", 0.0))
            name = str(data.get("skill_name", "unnamed-skill")).lower().replace(" ", "-")[:64]
            reason = str(data.get("reason", ""))[:256]
            return score, name, reason
        except Exception:
            pass
    return 0.0, "unnamed-skill", "Failed to assess"


async def generate_skill_md(task: str, solution: str, skill_name: str) -> str:
    """Generate SKILL.md content from task + solution."""
    title = skill_name.replace("-", " ").title()
    prompt = _GENERATE_SKILL_PROMPT.format(
        skill_name_title=title,
        task=task[:400],
        solution=solution[:2000],
    )
    return await _llm(prompt, timeout=90.0)


async def crystallize(
    task: str,
    solution: str,
    agent_id: str = "crystallizer",
    platform: str = "claude",
    force: bool = False,
) -> CrystallizationResult:
    """
    Full crystallization pipeline.
    Returns CrystallizationResult with skill_id if published.
    """
    # Step 1: assess reusability
    try:
        score, skill_name, reason = await assess(task, solution)
    except Exception as e:
        logger.warning("Assessment failed: %s", e)
        return CrystallizationResult(
            crystallized=False, skill_id=None, skill_name=None,
            reusability_score=0.0, reason=f"Assessment error: {e}"
        )

    if not force and score < CRYSTALLIZE_THRESHOLD:
        logger.info("Skill not crystallized: score %.2f < %.2f — %s", score, CRYSTALLIZE_THRESHOLD, reason)
        return CrystallizationResult(
            crystallized=False, skill_id=None, skill_name=skill_name,
            reusability_score=score, reason=reason
        )

    # Step 2: generate SKILL.md
    try:
        skill_content = await generate_skill_md(task, solution, skill_name)
    except Exception as e:
        logger.warning("Skill generation failed: %s", e)
        return CrystallizationResult(
            crystallized=False, skill_id=None, skill_name=skill_name,
            reusability_score=score, reason=f"Generation error: {e}"
        )

    # Step 3: publish to skill marketplace
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            base = f"http://localhost:{settings.server_port if hasattr(settings, 'server_port') else 8000}/api/v1"
            r = await c.post(f"{base}/skills/publish", json={
                "name": skill_name,
                "content": skill_content,
                "platform": platform,
                "agent_id": agent_id,
                "importance_score": min(1.0, score + 0.1),
            })
            r.raise_for_status()
            skill_id = r.json()["id"]
    except Exception as e:
        logger.warning("Skill publish failed: %s", e)
        # Return content even if publish failed — caller can save locally
        return CrystallizationResult(
            crystallized=True, skill_id=None, skill_name=skill_name,
            reusability_score=score, reason=reason, skill_content=skill_content
        )

    # Step 4: register in capability registry
    try:
        from app.services.capability_registry import get_registry
        task_type = _infer_task_type(task)
        get_registry().register(
            component=f"skill:{skill_name}",
            task_type=task_type,
            initial_score=score,
            description=f"Crystallized skill from cloud solution",
        )
    except Exception as e:
        logger.warning("Registry registration failed: %s", e)

    logger.info("Skill crystallized: %s (score=%.2f, id=%s)", skill_name, score, skill_id)
    return CrystallizationResult(
        crystallized=True, skill_id=skill_id, skill_name=skill_name,
        reusability_score=score, reason=reason, skill_content=skill_content
    )


def _infer_task_type(task: str) -> str:
    """Quick keyword-based task type inference (no LLM needed here)."""
    task_lower = task.lower()
    if any(w in task_lower for w in ["layout", "keyboard", "раскладк"]):
        return "layout_fix"
    if any(w in task_lower for w in ["log", "error", "filter", "лог"]):
        return "log_filter"
    if any(w in task_lower for w in ["code", "function", "class", "api", "implement"]):
        return "code_generation"
    if any(w in task_lower for w in ["review", "bug", "fix", "issue"]):
        return "code_review"
    if any(w in task_lower for w in ["summar", "brief", "short"]):
        return "text_summarization"
    if any(w in task_lower for w in ["deploy", "cloud", "server"]):
        return "code_generation"
    return "text_summarization"
