"""
Capability Registry API — query and update component capabilities.
"""

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.capability_registry import get_registry

router = APIRouter(prefix="/registry", tags=["registry"])


class UpdateRequest(BaseModel):
    component: str = Field(..., description="Component ID (e.g. 'qwen3:1.7b', 'cloud-llm', 'skill:fix-layout')")
    task_type: str = Field(..., description="Task type (e.g. 'layout_fix', 'code_generation')")
    success: bool
    description: str = Field("", description="Optional description of the capability")


class RegisterRequest(BaseModel):
    component: str
    task_type: str
    initial_score: float = Field(0.5, ge=0.0, le=1.0)
    description: str = ""


@router.get("/components")
async def list_components():
    """List all registered components with their capability scores."""
    return get_registry().components()


@router.get("/best")
async def best_for_task(
    task_type: str = Query(..., description="Task type to find best component for"),
    exclude: Optional[str] = Query(None, description="Comma-separated components to exclude"),
    top: int = Query(3, ge=1, le=10),
):
    """Return top components ranked by score for a given task type."""
    exclude_list = [e.strip() for e in exclude.split(",")] if exclude else []
    ranked = get_registry().best_for(task_type, exclude=exclude_list)
    return {
        "task_type": task_type,
        "ranked": [{"component": c, "score": round(s, 3)} for c, s in ranked[:top]],
    }


@router.get("/task_types")
async def list_task_types():
    """List all known task types."""
    return {"task_types": get_registry().task_types()}


@router.post("/update")
async def update_score(body: UpdateRequest):
    """Record a task outcome and update the component's capability score."""
    new_score = get_registry().update(
        component=body.component,
        task_type=body.task_type,
        success=body.success,
        description=body.description,
    )
    return {
        "component": body.component,
        "task_type": body.task_type,
        "success": body.success,
        "new_score": round(new_score, 3),
    }


@router.post("/register")
async def register_capability(body: RegisterRequest):
    """Register a new component capability."""
    get_registry().register(
        component=body.component,
        task_type=body.task_type,
        initial_score=body.initial_score,
        description=body.description,
    )
    score = get_registry().score(body.component, body.task_type)
    return {"component": body.component, "task_type": body.task_type, "score": round(score, 3)}


@router.get("/score")
async def get_score(
    component: str = Query(...),
    task_type: str = Query(...),
):
    """Get current score for a specific component+task_type pair."""
    score = get_registry().score(component, task_type)
    return {"component": component, "task_type": task_type, "score": round(score, 3)}
