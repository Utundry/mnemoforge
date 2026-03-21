from __future__ import annotations

import logging
import os
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import QdrantDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outcomes", tags=["outcomes"])


class OutcomeRequest(BaseModel):
    success: bool = Field(..., description="Whether the session outcome was successful")
    agent_id: str = Field(
        "",
        max_length=256,
        description="Optional agent identifier for audit trail (stored in learning events).",
    )
    project: str = Field(
        "",
        max_length=256,
        description="Optional project name for audit trail (stored in learning events).",
    )
    session_id: Optional[str] = Field(
        None,
        max_length=128,
        description="Session/episode id returned by /memories/context (links to memory_use events).",
    )
    memory_ids: list[UUID] = Field(
        default_factory=list,
        description="Optional explicit list of memory IDs to apply feedback to (overrides session lookup).",
    )
    boost: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Success boost. Default from OUTCOME_BOOST env (0.05).",
    )
    penalty: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Failure penalty. Default from OUTCOME_PENALTY env (0.03).",
    )


@router.post("")
async def record_outcome(body: OutcomeRequest, qdrant: QdrantDep, background_tasks: BackgroundTasks) -> dict:
    """
    Apply outcome-driven feedback to importance_score of memories used in a session.

    If memory_ids is empty and session_id is provided, the server resolves used memory_ids
    from Learning Ledger events of type 'memory_use' with matching episode_id.
    """
    memory_ids = list(body.memory_ids or [])

    if not memory_ids and body.session_id:
        try:
            from app.services.learning_store import get_learning_store

            store = get_learning_store()
            rows = await store.list_events(
                event_type="memory_use",
                episode_id=body.session_id,
                limit=200,
            )
            used: list[UUID] = []
            for r in rows:
                payload_json = r.get("payload_json") or "{}"
                try:
                    import json

                    payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
                except Exception:
                    payload = {}
                for raw in (payload.get("used_ids") or []):
                    try:
                        used.append(UUID(str(raw)))
                    except Exception:
                        continue
            # Preserve order, drop duplicates
            seen: set[UUID] = set()
            memory_ids = []
            for mid in used:
                if mid not in seen:
                    seen.add(mid)
                    memory_ids.append(mid)
        except Exception as exc:
            logger.debug("Outcome: session_id lookup failed (non-fatal): %s", exc)

    if not memory_ids:
        raise HTTPException(
            status_code=422,
            detail="No memory_ids provided and no resolvable session_id found.",
        )

    result = await qdrant.apply_outcome_feedback(
        memory_ids,
        success=body.success,
        boost=body.boost,
        penalty=body.penalty,
    )

    try:
        from app.services.event_emitter import emit
        background_tasks.add_task(
            emit,
            "outcome_recorded",
            agent_id=body.agent_id or "",
            project=body.project or "",
            transport="api",
            episode_id=body.session_id or "",
            context_signature="outcomes",
            payload={
                "success": body.success,
                "session_id": body.session_id,
                "updated": result.get("updated", 0),
                "skipped": result.get("skipped", 0),
                "memory_ids": [str(x) for x in memory_ids],
            },
        )
    except Exception as exc:
        logger.debug("Outcome: emit outcome_recorded skipped (non-fatal): %s", exc)

    return {
        "recorded": True,
        "success": body.success,
        "session_id": body.session_id,
        "memory_ids": [str(x) for x in memory_ids],
        "updated": result.get("updated", 0),
        "skipped": result.get("skipped", 0),
        "boost": body.boost if body.boost is not None else float(os.getenv("OUTCOME_BOOST", "0.05")),
        "penalty": body.penalty if body.penalty is not None else float(os.getenv("OUTCOME_PENALTY", "0.03")),
    }
