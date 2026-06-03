from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec
from app.services.stage_applicability_service import stage_allows_block


def _clean_text(value: object) -> str:
    return str(value or "").strip().lower()


@lru_cache(maxsize=1)
def _adherence_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/adherence_cues.json")
    except Exception:
        return {"default_max_cues": 0, "cues": []}


def _all_adherence_cues() -> list[dict[str, Any]]:
    cues = _adherence_spec().get("cues")
    return [cue for cue in cues if isinstance(cue, dict)] if isinstance(cues, list) else []


def adherence_cues_for_state(*, state: str, limit: int | None = None) -> list[dict[str, Any]]:
    state_text = _clean_text(state)
    max_items = limit or int(_adherence_spec().get("default_max_cues") or 3)
    selected: list[dict[str, Any]] = []
    for cue in _all_adherence_cues():
        if not _cue_allows_state(cue, state=state_text):
            continue
        selected.append(cue)
        if len(selected) >= max_items:
            break
    return selected


def adherence_cues_for_query(*, query: str, state: str = "", limit: int | None = None) -> list[dict[str, Any]]:
    text = _clean_text(query)
    if not text:
        return []
    max_items = limit or int(_adherence_spec().get("default_max_cues") or 3)
    scored: list[tuple[int, dict[str, Any]]] = []
    for cue in _all_adherence_cues():
        if state and not _cue_allows_state(cue, state=state):
            continue
        terms = [_clean_text(term) for term in cue.get("trigger_terms") or []]
        score = sum(1 for term in terms if term and term in text)
        if score:
            scored.append((score, cue))
    scored.sort(key=lambda pair: (pair[0], _severity_rank(pair[1].get("severity"))), reverse=True)
    result: list[dict[str, Any]] = []
    for score, cue in scored[:max_items]:
        ranked = dict(cue)
        ranked["_score"] = score
        result.append(ranked)
    return result


def expand_adherence_cue(ref: str, *, state: str = "") -> dict[str, Any] | None:
    cue_id = str(ref or "").strip()
    if cue_id.startswith("cue:"):
        cue_id = cue_id[len("cue:") :]
    if not cue_id:
        return None
    for cue in _all_adherence_cues():
        if str(cue.get("id") or "").strip() != cue_id:
            continue
        return {
            "ref": f"cue:{cue_id}",
            "cue": cue_id,
            "expanded_by": "explicit_ref",
            "stage_applicability": {
                "state": _clean_text(state),
                "allowed_in_state": _cue_allows_state(cue, state=state) if state else None,
            },
            "severity": cue.get("severity"),
            "authority_layer": cue.get("authority_layer"),
            "source": cue.get("source"),
            "scope": cue.get("scope") or [],
            "title": cue.get("title"),
            "summary": cue.get("summary"),
            "full_text": cue.get("full_text") or cue.get("summary"),
        }
    return None


def _cue_allows_state(cue: dict[str, Any], *, state: str) -> bool:
    cue_id = str(cue.get("id") or "").strip()
    if cue_id and not stage_allows_block(cue_id, state=state):
        return False
    scopes = {_clean_text(scope) for scope in cue.get("scope") or []}
    return not scopes or _clean_text(state) in scopes


def _severity_rank(value: object) -> int:
    return {"P0": 3, "P1": 2, "P2": 1}.get(str(value or "").strip().upper(), 0)
