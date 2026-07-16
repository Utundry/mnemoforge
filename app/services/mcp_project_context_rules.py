"""Project-context rule reference helpers for MCP facade responses."""
from __future__ import annotations

import re
from typing import Any


def project_rule_query_tokens(args: dict[str, Any]) -> set[str]:
    text = " ".join(str(args.get(key) or "") for key in ("query", "intent", "task", "project"))
    return {token for token in re.findall(r"[a-zA-Z0-9_+-]{3,}", text.casefold())}


def project_context_rule_refs(args: dict[str, Any]) -> list[dict[str, Any]]:
    project = str(args.get("project") or "").strip()
    status = str(args.get("status") or "active").strip().lower()
    if not project or status not in {"active", "all"}:
        return []
    tokens = project_rule_query_tokens(args)
    should_include_testing_rules = not tokens or bool(
        tokens
        & {
            "test",
            "tests",
            "testing",
            "pytest",
            "docker",
            "contour",
            "verification",
            "verify",
            "rules",
            "laws",
            "constraints",
        }
    )
    if not should_include_testing_rules:
        return []
    try:
        from app.services.task_execution_context_service import _project_testing_rule_refs

        refs = _project_testing_rule_refs(project)
    except Exception:
        return []
    return [
        {
            "id": ref.id,
            "title": ref.title,
            "status": ref.status,
            "scope": ref.scope,
            "project": project,
            "topic_path": ref.topic_path,
            "rationale": ref.rationale,
            "reason": ref.reason,
            "is_project_local": True,
            "source": "project_context",
        }
        for ref in refs
        if status == "all" or ref.status == status
    ]
