from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec
from app.services.stage_applicability_service import stage_allows_block, stage_applicability_metadata


def _clean_text(value: object) -> str:
    return str(value or "").strip().lower()


@lru_cache(maxsize=1)
def _cue_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("context/cues.json")
    except Exception:
        return {"default_max_cues": 0, "cues": []}


def _all_cues() -> list[dict[str, Any]]:
    cues = _cue_spec().get("cues")
    return [cue for cue in cues if isinstance(cue, dict)] if isinstance(cues, list) else []


def _public_cue(cue: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "cue": cue.get("id"),
        "severity": cue.get("severity"),
        "title": cue.get("title"),
        "summary": cue.get("summary"),
        "reason": reason,
        "expand_ref": cue.get("expand_ref") or f"cue:{cue.get('id')}",
    }


def context_cues_for_state(
    *,
    state: str,
    project: str = "",
    max_cues: int | None = None,
) -> list[dict[str, Any]]:
    del project  # Project-acquired cues can be merged here in a later slice.
    state_text = _clean_text(state)
    limit = max_cues or int(_cue_spec().get("default_max_cues") or 5)
    selected: list[dict[str, Any]] = []
    for cue in _all_cues():
        scopes = {_clean_text(item) for item in cue.get("scope") or []}
        cue_id = str(cue.get("id") or "").strip()
        if cue_id and not stage_allows_block(cue_id, state=state_text):
            continue
        if state_text in scopes or (state_text == "planning" and "planning" in scopes):
            selected.append(_public_cue(cue, reason=f"state:{state_text}"))
        if len(selected) >= limit:
            break
    return selected


def context_cues_for_query(
    *,
    query: str,
    project: str = "",
    state: str = "",
    max_cues: int | None = None,
) -> list[dict[str, Any]]:
    del project  # Project-acquired cues can be merged here in a later slice.
    text = _clean_text(query)
    if not text:
        return []
    limit = max_cues or int(_cue_spec().get("default_max_cues") or 5)
    scored: list[tuple[int, dict[str, Any]]] = []
    for cue in _all_cues():
        cue_id = str(cue.get("id") or "").strip()
        if state and cue_id and not stage_allows_block(cue_id, state=state):
            continue
        terms = [_clean_text(term) for term in cue.get("trigger_terms") or []]
        score = sum(1 for term in terms if term and term in text)
        if score:
            scored.append((score, cue))
    scored.sort(key=lambda pair: (pair[0], str(pair[1].get("severity") or "")), reverse=True)
    return [_public_cue(cue, reason="query_trigger") for _, cue in scored[:limit]]


def expand_context_cue(ref: str, *, project: str = "", state: str = "") -> dict[str, Any] | None:
    normalized = str(ref or "").strip()
    if normalized.startswith("cue:"):
        cue_id = normalized[len("cue:") :]
    else:
        cue_id = normalized
    cue_id = cue_id.strip()
    if not cue_id:
        return None
    for cue in _all_cues():
        if str(cue.get("id") or "").strip() == cue_id:
            applicability = stage_applicability_metadata(cue_id, state=state)
            return {
                "ref": f"cue:{cue_id}",
                "cue": cue_id,
                "project": project,
                "expanded_by": "explicit_ref",
                "stage_applicability": {key: value for key, value in applicability.items() if value not in (None, "", [], {})},
                "severity": cue.get("severity"),
                "scope": cue.get("scope") or [],
                "title": cue.get("title"),
                "summary": cue.get("summary"),
                "full_text": cue.get("full_text") or cue.get("summary"),
                "source": "context_cue_registry",
            }
    return None
