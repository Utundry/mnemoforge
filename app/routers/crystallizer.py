import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.dependencies import OllamaDep, QdrantDep
from app.services.skill_crystallizer import CRYSTALLIZE_THRESHOLD, assess, crystallize, generate_skill_md
from qdrant_client.http import models as qmodels

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/crystallizer", tags=["crystallizer"])


class CrystallizeRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=2000, description="Task that was solved")
    solution: str = Field(..., min_length=10, max_length=20000, description="Cloud LLM solution")
    agent_id: str = Field("crystallizer", max_length=256)
    platform: str = Field("claude", description="claude | codex | cursor | universal")
    force: bool = Field(False, description="Crystallize even if reusability score is low")


class AssessRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=2000)
    solution: str = Field(..., min_length=10, max_length=5000)


@router.post("/assess")
async def assess_solution(body: AssessRequest):
    """
    Assess whether a cloud LLM solution is worth crystallizing into a skill.
    Returns reusability score (0-1), suggested skill name, and reasoning.
    """
    score, skill_name, reason = await assess(body.task, body.solution)
    return {
        "reusability_score": round(score, 3),
        "skill_name": skill_name,
        "reason": reason,
        "will_crystallize": score >= CRYSTALLIZE_THRESHOLD,
        "threshold": CRYSTALLIZE_THRESHOLD,
    }


@router.post("/crystallize")
async def crystallize_solution(body: CrystallizeRequest):
    """
    Full crystallization pipeline: assess → generate SKILL.md → publish → register.
    If reusability score >= threshold, creates a skill in the marketplace automatically.
    """
    result = await crystallize(
        task=body.task,
        solution=body.solution,
        agent_id=body.agent_id,
        platform=body.platform,
        force=body.force,
    )
    return {
        "crystallized": result.crystallized,
        "skill_id": result.skill_id,
        "skill_name": result.skill_name,
        "reusability_score": round(result.reusability_score, 3),
        "reason": result.reason,
        "skill_content": result.skill_content,
    }


@router.post("/draft")
async def draft_skill(body: CrystallizeRequest):
    """
    Three-stage pipeline — stage 1+2 only (no auto-publish).
    Local LLM assesses reusability, GLM generates SKILL.md draft.
    The caller (main LLM / agent) reviews the draft and decides whether to publish
    via POST /skills/publish. This enables human-in-the-loop moderation.
    """
    # Stage 1: local LLM assesses reusability
    try:
        score, skill_name, reason = await assess(body.task, body.solution)
    except Exception as e:
        return {
            "draft_ready": False, "skill_name": None,
            "reusability_score": 0.0, "reason": f"Assessment error: {e}",
            "skill_content": None, "auto_publish_recommended": False,
        }

    if not body.force and score < CRYSTALLIZE_THRESHOLD:
        return {
            "draft_ready": False, "skill_name": skill_name,
            "reusability_score": round(score, 3), "reason": reason,
            "skill_content": None, "auto_publish_recommended": False,
        }

    # Stage 2: GLM (or local fallback) generates SKILL.md draft
    try:
        skill_content = await generate_skill_md(body.task, body.solution, skill_name)
    except Exception as e:
        return {
            "draft_ready": False, "skill_name": skill_name,
            "reusability_score": round(score, 3), "reason": f"Generation error: {e}",
            "skill_content": None, "auto_publish_recommended": False,
        }

    return {
        "draft_ready": True,
        "skill_name": skill_name,
        "reusability_score": round(score, 3),
        "reason": reason,
        "skill_content": skill_content,
        # Stage 3 hint: recommend auto-publish only for very high scores
        "auto_publish_recommended": score >= 0.85,
        "platform": body.platform,
        "agent_id": body.agent_id,
    }


@router.get("/threshold")
async def get_threshold():
    return {"crystallize_threshold": CRYSTALLIZE_THRESHOLD}


@router.post("/evolve")
async def evolve_skills(qdrant: QdrantDep, ollama: OllamaDep) -> dict:
    """
    Skill Evolver — spec §6. Runs suppression check and domain gap detection.
    Safe: never deletes, only suppresses. Suppression requires usage_count >= 10 AND usefulness_score < 0.15.
    """
    from app.routers.skills import _scroll_skills
    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType

    # 1. Load all skills (including suppressed for re-evaluation)
    all_skills = await _scroll_skills(qdrant, limit=500, include_suppressed=True)

    suppressed_ids: list[str] = []
    re_enabled_ids: list[str] = []

    for skill in all_skills:
        usage = skill.get("usage_count", 0)
        helpful = skill.get("helpful_count", 0)
        currently_suppressed = skill.get("suppressed", False)

        if usage >= 10:
            # Laplace smoothing: usefulness = helpful / (usage + 3)
            usefulness = round(helpful / (usage + 3), 3)
            if usefulness < 0.15 and not currently_suppressed:
                await qdrant._client.set_payload(
                    collection_name=qdrant._collection,
                    payload={"suppressed": True, "usefulness_score": usefulness},
                    points=[skill["id"]],
                )
                suppressed_ids.append(skill["id"])
                logger.info("Suppressed skill %s usefulness=%.3f usage=%d", skill["id"], usefulness, usage)
            elif usefulness >= 0.3 and currently_suppressed:
                # Re-enable if score recovered
                await qdrant._client.set_payload(
                    collection_name=qdrant._collection,
                    payload={"suppressed": False, "usefulness_score": usefulness},
                    points=[skill["id"]],
                )
                re_enabled_ids.append(skill["id"])
                logger.info("Re-enabled skill %s usefulness=%.3f", skill["id"], usefulness)

    # 2. Detect domain gaps from skill_outcome memories
    must = [qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill_outcome"))]
    outcomes, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=must),
        limit=200,
        with_payload=True,
        with_vectors=False,
    )

    domain_gap_counts: Counter = Counter()
    for o in outcomes:
        content = o.payload.get("content", "")
        m = re.search(r'missing_domains=([^\s|]+)', content)
        if m and m.group(1):
            for d in m.group(1).split(","):
                d = d.strip()
                if d:
                    domain_gap_counts[d] += 1

    # Domains already covered by active skills
    covered_domains: set[str] = set()
    for s in all_skills:
        if not s.get("suppressed"):
            covered_domains.update(s.get("domain_tags", []))

    gaps_detected: list[str] = []
    for domain, count in domain_gap_counts.most_common(10):
        if count >= 3 and domain not in covered_domains:
            gaps_detected.append(domain)

    # 3. Crystallize recurring successful_patterns from dialogue signals
    pattern_must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="dialogue_signal")),
    ]
    signals, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=pattern_must),
        limit=200,
        with_payload=True,
        with_vectors=False,
    )

    pattern_counts: Counter = Counter()
    for sig in signals:
        content = sig.payload.get("content", "")
        m = re.search(r'successful_pattern=([^|]+)', content)
        if m:
            for p in m.group(1).split(","):
                p = p.strip()
                if p and len(p) > 10:
                    pattern_counts[p] += 1

    # Already-crystallized patterns (avoid duplicates)
    crystallized_must = [
        qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill_evolution_log")),
    ]
    prev_logs, _ = await qdrant._client.scroll(
        collection_name=qdrant._collection,
        scroll_filter=qmodels.Filter(must=crystallized_must),
        limit=50,
        with_payload=True,
        with_vectors=False,
    )
    already_crystallized: set[str] = set()
    for log in prev_logs:
        c = log.payload.get("content", "")
        m = re.search(r'crystallized_patterns=([^\s]+)', c)
        if m:
            already_crystallized.update(m.group(1).split(","))

    crystallized_patterns: list[str] = []
    for pattern, count in pattern_counts.most_common(3):
        if count < 2 or pattern in already_crystallized:
            continue
        try:
            result = await crystallize(
                task=f"Recurring successful pattern: {pattern}",
                solution=f"Pattern observed {count} times in sessions: {pattern}",
                agent_id="skill-evolver",
                platform="claude",
                force=False,
            )
            if result.crystallized:
                crystallized_patterns.append(pattern)
                logger.info("Crystallized pattern '%s' -> skill '%s'", pattern, result.skill_name)
        except Exception as e:
            logger.warning("Crystallize failed for pattern '%s': %s", pattern, e)

    # 4. Log evolution report
    now = datetime.now(timezone.utc).isoformat()
    report_content = (
        f"skill_evolution_log ts={now} "
        f"total_skills={len(all_skills)} "
        f"suppressed={len(suppressed_ids)} "
        f"re_enabled={len(re_enabled_ids)} "
        f"gaps_detected={','.join(gaps_detected[:5]) or 'none'} "
        f"crystallized_patterns={','.join(crystallized_patterns) or 'none'}"
    )
    vector = await ollama.embed(report_content)
    report_mem = MemoryCreate(
        content=report_content,
        agent_id="skill-evolver",
        memory_type=MemoryType.context,
        category="skill_evolution_log",
        importance_score=0.4,
        source="crystallizer/evolve",
        tags=["skill_evolution_log"],
        session_id=None,
    )
    await qdrant.insert(report_mem, vector)

    logger.info(
        "Skill evolution: suppressed=%d re_enabled=%d gaps=%s crystallized=%s",
        len(suppressed_ids), len(re_enabled_ids), gaps_detected, crystallized_patterns,
    )

    return {
        "suppressed": suppressed_ids,
        "re_enabled": re_enabled_ids,
        "gaps_detected": gaps_detected,
        "crystallized_patterns": crystallized_patterns,
        "total_skills": len(all_skills),
        "evolved_at": now,
    }


# ── Job queue handler ─────────────────────────────────────────────────────────

async def _evolve_handler(payload: dict) -> dict:
    """Job queue handler for auto-triggered skill evolution."""
    from app.dependencies import get_qdrant, get_ollama
    result = await evolve_skills(get_qdrant(), get_ollama())
    logger.info("Auto-evolve completed: %s", result)
    return result
