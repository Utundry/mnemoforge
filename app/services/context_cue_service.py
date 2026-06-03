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
    payload = {
        "cue": cue.get("id"),
        "severity": cue.get("severity"),
        "title": cue.get("title"),
        "summary": cue.get("summary"),
        "reason": reason,
        "expand_ref": cue.get("expand_ref") or f"cue:{cue.get('id')}",
    }
    for key in ("authority_layer", "source"):
        if cue.get(key) not in (None, "", []):
            payload[key] = cue.get(key)
    return payload


def context_cues_for_state(
    *,
    state: str,
    project: str = "",
    max_cues: int | None = None,
    governed_laws: list[Any] | None = None,
) -> list[dict[str, Any]]:
    state_text = _clean_text(state)
    limit = max_cues or int(_cue_spec().get("default_max_cues") or 5)
    selected: list[dict[str, Any]] = []
    selected.extend(
        _public_cue(cue, reason=f"governed_law:{state_text}")
        for cue in governed_law_cues(
            governed_laws or [],
            project=project,
            state=state_text,
            limit=limit,
        )
    )
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
    governed_laws: list[Any] | None = None,
) -> list[dict[str, Any]]:
    text = _clean_text(query)
    if not text:
        return []
    limit = max_cues or int(_cue_spec().get("default_max_cues") or 5)
    scored: list[tuple[int, dict[str, Any]]] = []
    for cue in governed_law_cues(governed_laws or [], project=project, state=state, limit=limit):
        haystack = _clean_text(" ".join(str(cue.get(key) or "") for key in ("title", "summary", "full_text")))
        terms = [_clean_text(term) for term in cue.get("trigger_terms") or []]
        score = sum(1 for term in terms if term and term in text)
        if not score and any(token and token in haystack for token in text.split()):
            score = 1
        if score:
            scored.append((score, cue))
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


def governed_law_cues(
    laws: list[Any],
    *,
    project: str = "",
    state: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for law in laws:
        cue = governed_law_to_cue(law, current_project=project)
        if not cue:
            continue
        if state and not _governed_law_allows_state(cue, state=state):
            continue
        selected.append(cue)
        if limit and len(selected) >= limit:
            break
    return selected


def governed_law_to_cue(law: Any, *, current_project: str = "") -> dict[str, Any] | None:
    data = law if isinstance(law, dict) else getattr(law, "model_dump", lambda **_: {})()
    if not isinstance(data, dict):
        return None
    status = _clean_text(data.get("status"))
    if status not in {"active", "user_confirmed"}:
        return None
    law_id = str(data.get("id") or data.get("law_id") or "").strip()
    if not law_id:
        return None
    scope = _clean_text(data.get("scope") or "project")
    law_project = str(data.get("project") or "").strip()
    title = str(data.get("title") or "Governed law").strip()
    statement = str(data.get("statement") or data.get("summary") or "").strip()
    rationale = str(data.get("rationale") or "").strip()
    tags = [str(tag).strip() for tag in data.get("tags") or [] if str(tag).strip()]
    ref = f"law:{law_project}:{law_id}" if law_project else f"law:{law_id}"
    return {
        "id": ref,
        "severity": _governed_law_severity(tags=tags, scope=scope),
        "authority_layer": _law_authority_layer(scope=scope, project=law_project, current_project=current_project, tags=tags),
        "scope": [scope],
        "title": title,
        "summary": rationale or statement[:240],
        "expand_ref": ref,
        "trigger_terms": _governed_law_trigger_terms(title=title, statement=statement, rationale=rationale, tags=tags),
        "full_text": statement,
        "source": "governed_law_db",
        "tags": tags,
    }


def _law_authority_layer(*, scope: str, project: str, current_project: str, tags: list[str]) -> str:
    tag_text = " ".join(tags).casefold()
    if scope in {"meta", "principle"} or "canonical" in tag_text:
        return "canonical_principle"
    if scope in {"domain", "family"}:
        return "cross_project_rule"
    if project and current_project and project == current_project:
        return "project_rule"
    if project:
        return "external_project_rule"
    return "governed_rule"


def _governed_law_severity(*, tags: list[str], scope: str) -> str:
    lowered = {tag.casefold() for tag in tags}
    if "p0" in lowered or scope in {"meta", "principle"}:
        return "P0"
    if "p2" in lowered:
        return "P2"
    return "P1"


def _governed_law_trigger_terms(*, title: str, statement: str, rationale: str, tags: list[str]) -> list[str]:
    words = []
    for text in (title, statement, rationale, " ".join(tags)):
        words.extend(token for token in _clean_text(text).replace("_", " ").replace("-", " ").split() if len(token) > 3)
    return list(dict.fromkeys(words))[:24]


def _governed_law_allows_state(cue: dict[str, Any], *, state: str) -> bool:
    tags = {_clean_text(tag) for tag in cue.get("tags") or []}
    state_text = _clean_text(state)
    explicit_states = {
        tag.split(":", 1)[1]
        for tag in tags
        if tag.startswith("stage:") or tag.startswith("state:")
    }
    return not explicit_states or state_text in explicit_states


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
