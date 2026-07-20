from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from app.services.mcp_workflow_specs import load_named_json_spec, workflow_spec_cache


@workflow_spec_cache(maxsize=1)
def _guardrail_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("workflow/spec_edit_guardrail.json")
    except Exception:
        return {"checked_paths": [], "forbidden_terms": []}


def _clean(value: object) -> str:
    return str(value or "").casefold().strip()


def _relative_spec_path(path: Path, *, spec_root: Path) -> str:
    return path.relative_to(spec_root).as_posix()


def _path_is_checked(relative_path: str) -> bool:
    patterns = [str(item or "") for item in _guardrail_spec().get("checked_paths") or [] if str(item or "").strip()]
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def _allowed_by_context(content: str, term_index: int, allowed_terms: list[str]) -> bool:
    if not allowed_terms:
        return False
    window = content[max(0, term_index - 180) : term_index + 180].casefold()
    return any(_clean(term) in window for term in allowed_terms if _clean(term))


def audit_universal_spec_runtime_leaks(*, spec_root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in sorted(spec_root.rglob("*.json")):
        relative = _relative_spec_path(path, spec_root=spec_root)
        if not _path_is_checked(relative):
            continue
        content = path.read_text(encoding="utf-8")
        lowered = content.casefold()
        for rule in _guardrail_spec().get("forbidden_terms") or []:
            if not isinstance(rule, dict):
                continue
            term = str(rule.get("term") or "").strip()
            if not term:
                continue
            index = lowered.find(term.casefold())
            if index < 0:
                continue
            allowed_terms = [str(item) for item in rule.get("allowed_context_terms") or []]
            if _allowed_by_context(content, index, allowed_terms):
                continue
            findings.append(
                {
                    "path": relative,
                    "term": term,
                    "reason": rule.get("reason") or "Project-specific runtime detail in universal spec.",
                }
            )
    return findings
