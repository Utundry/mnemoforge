from __future__ import annotations

import re
from typing import Any, Callable


TaskIdDetector = Callable[[str], Any]


def ask_project_response_format(args: dict[str, Any]) -> str:
    requested = str(args.get("response_format") or "").strip().lower()
    if requested in {"answer", "diagnostic", "json"}:
        return requested
    client_profile = str(args.get("client_profile") or "").strip().lower()
    if client_profile in {"local", "local_model", "small", "small_context", "slm", "weak"}:
        return "answer"
    return "answer"


def ask_project_query_text(args: dict[str, Any]) -> str:
    return str(args.get("question") or args.get("query") or args.get("intent") or "").strip()


def select_ask_project_lexical_route(
    args: dict[str, Any],
    *,
    extract_task_id_like: TaskIdDetector,
) -> dict[str, Any]:
    question = ask_project_query_text(args)
    text = lexical_text(question)
    project = str(args.get("project") or args.get("project_id") or "mnemoforge").strip() or "mnemoforge"
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    response_format = ask_project_response_format(args)
    read_lookup = any(term in text for term in _READ_LOOKUP_TERMS)
    artifact_lookup = read_lookup and any(term in text for term in _ARTIFACT_LOOKUP_TERMS)
    terse_topic_lookup = is_terse_topic_lookup(question)

    route = {
        "facade": "project_context",
        "reason": "General project question maps to project_context.",
        "confidence": 0.7,
        "response_format": response_format,
        "payload": {
            "project": project,
            "intent": question,
            "detail": detail,
            "response_format": response_format,
            "limit": int(args.get("limit") or 20),
        },
        "guardrail": "",
    }

    open_work_lookup = any(
        term in text
        for term in (
            "next",
            "priority",
            "open work",
            "open tasks",
            "active tasks",
            "list open tasks",
            "list active tasks",
            "list open task",
            "list active task",
            "list open",
            "list active",
            "list open tasks",
            "list_open_tasks",
            "what should i do",
            "continue",
            "backlog",
        )
    )

    if extract_task_id_like(question):
        route.update(
            facade="project_context",
            reason="Question contains a full or partial task id; route to project_context task lookup.",
            confidence=0.9,
        )
        route["structural_match"] = True
    elif open_work_lookup:
        route.update(
            facade="project_work",
            reason="Question asks for next/open project work; route to project_work read-only planning.",
            confidence=0.86,
        )
    elif artifact_lookup or terse_topic_lookup:
        route.update(
            facade="project_context",
            reason="Read-only topic or artifact lookup maps to project_context artifact search.",
            confidence=0.9 if artifact_lookup else 0.82,
        )
        route["structural_match"] = True
        route["payload"]["artifact_lookup"] = True
        artifact_type = artifact_lookup_type(text)
        if artifact_type:
            route["payload"]["artifact_type"] = artifact_type
    elif any(term in text for term in ("memory", "find in memory", "search memory", "recall", "remember", "memory_store", "memory_search")):
        route.update(
            facade="project_context",
            reason="Question asks to find/search/recall memory content; route to project_context for memory lookup.",
            confidence=0.88,
        )
    elif any(term in text for term in ("test", "tests", "verify", "verification", "health", "restart", "smoke", "failed", "failure")):
        route.update(
            facade="project_verify",
            reason="Question asks about tests, health, restart, or verification; route to project_verify.",
            confidence=0.84,
        )
    elif any(term in text for term in ("rule", "rules", "law", "laws", "constraint", "constraints", "forget")):
        route.update(
            facade="project_context",
            reason="Question asks about rules or constraints; route through project_context so it can delegate to project_rules safely.",
            confidence=0.82,
        )
    elif any(term in text for term in ("ready", "readiness", "usable", "used yet", "bootstrap", "onboard")):
        route.update(
            facade="project_context",
            reason="Question asks about readiness or usability; route to project_context readiness handling.",
            confidence=0.82,
        )

    if any(term in text for term in _MUTATION_TERMS) and not read_lookup:
        route["guardrail"] = "Mutation-like question detected; ask_project will not set allow_mutation=true."
        if any(term in text for term in ("save", "record", "checkpoint", "close")):
            route.update(
                facade="project_capture",
                reason="Mutation-like capture request is routed to project_capture with allow_mutation=false.",
                confidence=max(float(route["confidence"]), 0.86),
            )

    route["payload"].update(
        {
            "project": project,
            "intent": question,
            "detail": detail,
            "response_format": response_format,
            "allow_mutation": False,
        }
    )
    if route["facade"] == "project_context":
        route["payload"].pop("allow_mutation", None)
    return route


def lexical_text(question: str) -> str:
    # Normalize tool-like phrases so list_open_tasks remains semantic text.
    return re.sub(r"[_\-/\.]+", " ", str(question or "")).casefold()


def is_terse_topic_lookup(question: str) -> bool:
    text = lexical_text(question).strip()
    if not text:
        return False
    if any(term in text for term in _MUTATION_TERMS):
        return False
    if any(term in text for term in _PROJECT_CONTROL_TERMS):
        return False
    words = text.split()
    return 1 <= len(words) <= 5 and any(any(ch.isalnum() for ch in word) for word in words)


def artifact_lookup_type(text: str) -> str:
    asks_tasks = any(term in text for term in ("task", "tasks", "\u0437\u0430\u0434\u0430\u0447"))
    asks_improvements = any(term in text for term in ("improvement", "improvements", "\u0443\u043b\u0443\u0447\u0448"))
    if asks_tasks and not asks_improvements:
        return "task"
    if asks_improvements and not asks_tasks:
        return "improvement"
    return ""


_READ_LOOKUP_TERMS = (
    "find",
    "search",
    "show",
    "list",
    "lookup",
    "look up",
    "read",
    "get",
    "details",
    "detail",
    "\u0432\u044b\u0432\u0435\u0434\u0438",
    "\u043f\u043e\u043a\u0430\u0436\u0438",
    "\u043d\u0430\u0439\u0434\u0438",
    "\u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0439",
    "\u0434\u0435\u0442\u0430\u043b\u0438",
)

_ARTIFACT_LOOKUP_TERMS = (
    "task",
    "tasks",
    "improvement",
    "improvements",
    "work item",
    "work items",
    "artifact",
    "artifacts",
    "\u0437\u0430\u0434\u0430\u0447",
    "\u0443\u043b\u0443\u0447\u0448",
)

_PROJECT_CONTROL_TERMS = (
    "next",
    "priority",
    "continue",
    "ready",
    "readiness",
    "usable",
    "verify",
    "verification",
    "health",
    "restart",
    "rule",
    "rules",
    "law",
    "laws",
    "constraint",
    "constraints",
)

_MUTATION_TERMS = (
    "save",
    "record",
    "close",
    "resolve",
    "delete",
    "promote",
    "approve",
    "create task",
    "write",
)
