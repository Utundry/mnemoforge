from __future__ import annotations

from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec, workflow_spec_cache


@workflow_spec_cache(maxsize=1)
def _evidence_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/evidence_classification.json")
    except Exception:
        return {"default_kind": "unspecified", "classes": []}


def _clean(value: object) -> str:
    return str(value or "").casefold().strip()


def classify_evidence_items(items: list[str]) -> dict[str, Any]:
    text = "\n".join(str(item or "") for item in items).casefold()
    if not text.strip():
        return {"kind": _evidence_spec().get("default_kind") or "unspecified", "evidence_count": 0}

    matches: list[dict[str, Any]] = []
    for item in _evidence_spec().get("classes") or []:
        if not isinstance(item, dict):
            continue
        markers = [_clean(marker) for marker in item.get("markers") or [] if _clean(marker)]
        matched = [marker for marker in markers if marker in text]
        if not matched:
            continue
        matches.append(
            {
                "kind": item.get("kind"),
                "summary": item.get("summary"),
                "matched_markers": matched[:5],
                "verification_evidence": bool(item.get("verification_evidence")),
                "live_diagnostic": bool(item.get("live_diagnostic")),
            }
        )
    if not matches:
        return {"kind": _evidence_spec().get("default_kind") or "unspecified", "evidence_count": len(items)}
    primary = matches[0]
    if len(matches) > 1:
        primary = {
            "kind": "mixed",
            "summary": "Evidence contains multiple evidence classes; keep verification and live diagnostic records distinct.",
            "verification_evidence": any(bool(item.get("verification_evidence")) for item in matches),
            "live_diagnostic": any(bool(item.get("live_diagnostic")) for item in matches),
        }
    return {
        **primary,
        "evidence_count": len(items),
        "matches": matches,
    }
