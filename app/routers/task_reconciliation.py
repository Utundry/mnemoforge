from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.task_reconciliation_service import get_task_reconciliation_store

router = APIRouter(prefix="/task-reconciliation", tags=["task-reconciliation"])


class TaskReconciliationReviewRequest(BaseModel):
    target_task_ref: str
    implemented_task_ref: str = ""
    decision: str
    reason: str
    acted_by: str = "codex"
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/packet")
async def get_reconciliation_packet(target_task_ref: str = Query(...)):
    return get_task_reconciliation_store().packet_for_target(target_task_ref)


@router.post("/review")
async def review_task_reconciliation(body: TaskReconciliationReviewRequest):
    try:
        decision = get_task_reconciliation_store().record_decision(
            target_task_ref=body.target_task_ref,
            implemented_task_ref=body.implemented_task_ref,
            decision=body.decision,
            reason=body.reason,
            acted_by=body.acted_by,
            evidence_refs=body.evidence_refs,
            metadata=body.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    packet = get_task_reconciliation_store().packet_for_target(body.target_task_ref)
    return {"decision_record": decision, "packet": packet, "source_of_truth": "sqlite"}
