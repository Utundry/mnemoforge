from __future__ import annotations

import re
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec


def explicit_adherence_cue_query(query: str) -> bool:
    text = _normalized_query(query)
    if not text:
        return False
    for route in _simple_get_routes():
        if str(route.get("id") or "") != "adherence_cues":
            continue
        return any(_term_matches(text, str(term or "")) for term in route.get("trigger_terms") or [])
    return False


def adherence_query_next_action(*, has_project: bool) -> str:
    if not has_project:
        return "Call get again with project set so governed project laws can be included with adherence cues."
    return "Review the cue refs; expand a cue ref only when full text is needed."


def _simple_get_routes() -> list[dict[str, Any]]:
    try:
        spec = load_named_json_spec("search/simple_get_routes.json")
    except Exception:
        spec = {}
    routes = spec.get("routes")
    return [route for route in routes if isinstance(route, dict)] if isinstance(routes, list) else []


def _normalized_query(query: str) -> str:
    return re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()


def _term_matches(text: str, term: str) -> bool:
    value = str(term or "").strip().casefold()
    if not value:
        return False
    if re.fullmatch(r"[\w ]+", value, flags=re.UNICODE):
        return re.search(rf"\b{re.escape(value)}\b", text, flags=re.UNICODE) is not None
    return value in text
