from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec


@lru_cache(maxsize=1)
def _stage_applicability_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/stage_applicability.json")
    except Exception:
        return {"default_policy": "show", "blocks": {}}


def _normalized_stage(value: object) -> str:
    return re.sub(r"[_\-/\.]+", " ", str(value or "")).casefold().strip()


def _normalized_stage_set(values: object) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {_normalized_stage(item) for item in values if str(item or "").strip()}


def _default_visibility(spec: dict[str, Any]) -> bool:
    return str(spec.get("default_policy") or "show").casefold().strip() != "hide"


def stage_allows_block(block_id: str, *, state: str, default: bool | None = None) -> bool:
    """Return whether a response block should be visible in the current FSM stage."""
    spec = _stage_applicability_spec()
    effective_default = _default_visibility(spec) if default is None else default
    blocks = spec.get("blocks") if isinstance(spec.get("blocks"), dict) else {}
    block = blocks.get(block_id) if isinstance(blocks.get(block_id), dict) else {}
    if not block:
        return effective_default

    state_text = _normalized_stage(state)
    if state_text in _normalized_stage_set(block.get("hide_in")):
        return False
    show_in = _normalized_stage_set(block.get("show_in"))
    if show_in:
        return state_text in show_in
    return effective_default


def stage_applicability_metadata(block_id: str, *, state: str, default: bool | None = None) -> dict[str, Any]:
    spec = _stage_applicability_spec()
    blocks = spec.get("blocks") if isinstance(spec.get("blocks"), dict) else {}
    block = blocks.get(block_id) if isinstance(blocks.get(block_id), dict) else {}
    state_text = _normalized_stage(state)
    state_known = bool(state_text)
    allowed = stage_allows_block(block_id, state=state_text, default=default) if state_known else True
    return {
        "block": block_id,
        "state": state_text or None,
        "state_known": state_known,
        "allowed_in_state": allowed,
        "why": block.get("why") if block else None,
        "show_in": block.get("show_in") or [],
        "hide_in": block.get("hide_in") or [],
    }
