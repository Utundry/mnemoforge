"""
Task Router API — decide which component should handle a task.
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.dependencies import QdrantDep
from app.services.task_router import decide

router = APIRouter(prefix="/router", tags=["router"])


class DecideRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=2000, description="Task description in natural language")
    task_type: Optional[str] = Field(None, description="Override auto-classification")
    preferred_tier: Optional[str] = Field(None, description="Force tier: 'local' | 'cloud' | 'skill' | 'reference'")


class ReferenceItem(BaseModel):
    id: str
    name: str
    description: str
    reference_url: Optional[str] = None


class CorrectionHint(BaseModel):
    actual_type: str
    count: int
    correction_rate: float


class DecideResponse(BaseModel):
    task_type: str
    component: str
    score: float
    tier: str  # "skill" | "local" | "cloud" | "reference"
    reasoning: str
    alternatives: list
    confidence: float
    cloud_fallbacks: list = []  # ranked fallback cloud models when tier=cloud
    references: list[ReferenceItem] = []  # populated when tier=reference
    correction_hints: list[CorrectionHint] = []  # Ivanov's feedback: frequent corrections for this task_type


@router.post("/decide", response_model=DecideResponse)
async def decide_routing(body: DecideRequest, qdrant: QdrantDep):
    """
    Classify a task and return the optimal component to handle it.

    Returns component name, tier (local/skill/cloud/reference), score, and reasoning.
    When tier=reference, the 'references' field contains pinned skills with endpoints.
    The caller is responsible for executing the task and recording the outcome
    via POST /tracker/record.
    """
    decision = await decide(
        task=body.task,
        task_type=body.task_type,
        preferred_tier=body.preferred_tier,
    )

    # When out-of-domain, fetch pinned reference skills
    references: list[ReferenceItem] = []
    if decision.tier == "reference":
        try:
            from qdrant_client.http import models as qmodels
            results, _ = await qdrant._client.scroll(
                collection_name=qdrant._collection,
                scroll_filter=qmodels.Filter(must=[
                    qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value="skill")),
                    qmodels.FieldCondition(key="pinned", match=qmodels.MatchValue(value=True)),
                ]),
                limit=10,
                with_payload=True,
                with_vectors=False,
            )
            for r in results:
                p = r.payload
                references.append(ReferenceItem(
                    id=str(r.id),
                    name=p.get("skill_name", ""),
                    description=p.get("skill_description", ""),
                    reference_url=p.get("reference_url"),
                ))
        except Exception:
            pass

    # Load Ivanov's correction signals: where was this task_type frequently misclassified?
    correction_hints: list[CorrectionHint] = []
    try:
        from app.services.performance_tracker import get_tracker
        corrections = get_tracker().corrections(task_type=decision.task_type, min_count=2)
        for c in corrections[:3]:  # top 3 corrections
            correction_hints.append(CorrectionHint(
                actual_type=c["actual_type"],
                count=c["count"],
                correction_rate=c["correction_rate"],
            ))
        # Amend reasoning when strong correction signal exists
        if correction_hints and correction_hints[0].correction_rate >= 0.3:
            top = correction_hints[0]
            decision.reasoning += (
                f" ⚠ Note: {int(top.correction_rate * 100)}% of past '{decision.task_type}' tasks "
                f"were corrected to '{top.actual_type}' — consider routing there instead."
            )
    except Exception:
        pass

    return DecideResponse(
        task_type=decision.task_type,
        component=decision.component,
        score=decision.score,
        tier=decision.tier,
        reasoning=decision.reasoning,
        alternatives=[{"component": c, "score": round(s, 3)} for c, s in decision.alternatives],
        confidence=decision.confidence,
        cloud_fallbacks=decision.cloud_fallbacks,
        references=references,
        correction_hints=correction_hints,
    )


@router.get("/policy")
async def get_policy():
    """Return current routing thresholds."""
    from app.services.task_router import LOCAL_THRESHOLD, SKILL_THRESHOLD
    return {
        "skill_threshold": SKILL_THRESHOLD,
        "local_threshold": LOCAL_THRESHOLD,
        "priority": ["skill", "local", "cloud"],
        "description": "Tasks routed to cheapest capable component. Scores below threshold escalate to next tier.",
    }
