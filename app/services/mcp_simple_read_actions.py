from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote


GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
ProjectExpertQueryCallback = Callable[[str, dict[str, Any], str | None], Awaitable[dict[str, Any]]]
TaskRefCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
PublicErrorCallback = Callable[[Exception], str]
TaskIdDetector = Callable[[str], Any]


@dataclass(frozen=True)
class SimpleReadDependencies:
    post: PostCallback
    query_project_expert: ProjectExpertQueryCallback
    extract_task_id_like: TaskIdDetector


@dataclass(frozen=True)
class PublicRefDependencies:
    get: GetCallback
    get_task_context: TaskRefCallback
    public_error_message: PublicErrorCallback


async def build_simple_public_ref_response(
    *,
    api_base: str,
    args: dict[str, Any],
    dependencies: PublicRefDependencies,
) -> dict[str, Any] | None:
    address = public_ref_address(args)
    if not address:
        return None

    kind = address["kind"]
    project = address.get("project") or str(args.get("project") or "mnemoforge")
    local_id = address.get("local_id") or ""
    requested_ref = str(args.get("ref") or "").strip()
    normalized_ref = address.get("artifact_key") or f"{kind}:{project}:{local_id}"
    try:
        if kind == "task":
            result = await dependencies.get_task_context(
                api_base,
                {
                    "project": project,
                    "task_id": local_id,
                    "detail": str(args.get("detail") or "compact"),
                    "include_handoffs": True,
                    "agent_id": str(args.get("agent_id") or "").strip(),
                    "limit": int(args.get("limit") or 10),
                },
            )
            next_safe_action = result.get("next_safe_action") or "Review task context before claiming or editing."
        elif kind in {"improvement", "artifact"}:
            artifact_key = address.get("artifact_key") or f"{kind}:{project}:{local_id}"
            result = await dependencies.get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
            next_safe_action = "Review this read-only artifact before choosing any mutating mailbox form."
        elif kind == "law":
            result = await dependencies.get(api_base, f"/laws/{quote(local_id, safe='')}")
            next_safe_action = "Review this read-only law before proposing revisions or candidates."
        elif kind == "rule_candidate":
            result = await dependencies.get(api_base, f"/laws/candidates/{quote(local_id, safe='')}")
            next_safe_action = "Review this read-only rule candidate before any promotion, revision, or review action."
        elif kind == "memory":
            result = await dependencies.get(api_base, f"/memories/{quote(local_id, safe='')}")
            next_safe_action = "Review this read-only memory before creating new facts or updates."
        else:
            return _public_ref_envelope(
                args=args,
                project=project,
                normalized_ref=normalized_ref,
                requested_ref=requested_ref,
                kind=kind,
                receipt={
                    "status": "unsupported_ref_kind",
                    "message": f"Mailbox public ref kind is not yet mapped to a read-only resolver: {kind}",
                    "supported_ref_kinds": ["task", "improvement", "artifact", "law", "rule_candidate", "memory"],
                    "next_safe_action": "Use mailbox_state for available forms, or ask_project/project_work for natural read-only lookup.",
                },
            )
    except Exception as exc:
        return _public_ref_envelope(
            args=args,
            project=project,
            normalized_ref=normalized_ref,
            requested_ref=requested_ref,
            kind=kind,
            receipt={
                "status": "not_found",
                "message": dependencies.public_error_message(exc),
                "next_safe_action": "Verify the public ref from the latest list/open/context result, then request mailbox_get again.",
            },
        )

    return _public_ref_envelope(
        args=args,
        project=project,
        normalized_ref=normalized_ref,
        requested_ref=requested_ref,
        kind=kind,
        receipt={
            "status": "accepted",
            "message": "Public mailbox reference resolved through a read-only handler.",
            "resource_kind": kind,
            "next_safe_action": next_safe_action,
        },
        result=result,
    )


def public_ref_address(args: dict[str, Any]) -> dict[str, str] | None:
    ref = str(args.get("ref") or "").strip()
    if not ref or ref.startswith("mailbox_state:"):
        return None

    parts = ref.split(":")
    if parts and parts[0] == "mailbox_get":
        parts = parts[1:]
    if not parts:
        return None

    default_project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    kind = parts[0].strip()
    if kind == "artifact" and len(parts) >= 4:
        artifact_type = parts[1].strip()
        project = parts[2].strip() or default_project
        local_id = ":".join(parts[3:]).strip()
        if artifact_type and local_id:
            return {
                "kind": "artifact",
                "artifact_type": artifact_type,
                "project": project,
                "local_id": local_id,
                "artifact_key": f"{artifact_type}:{project}:{local_id}",
            }
    if kind in {"task", "improvement"} and len(parts) >= 3:
        project = parts[1].strip() or default_project
        local_id = ":".join(parts[2:]).strip()
        if local_id:
            return {
                "kind": kind,
                "artifact_type": kind,
                "project": project,
                "local_id": local_id,
                "artifact_key": f"{kind}:{project}:{local_id}",
            }
    if kind in {"law", "rule_candidate", "candidate", "memory"} and len(parts) >= 2:
        project = parts[1].strip() if len(parts) >= 3 else default_project
        local_id = ":".join(parts[2:] if len(parts) >= 3 else parts[1:]).strip()
        if local_id:
            normalized_kind = "rule_candidate" if kind == "candidate" else kind
            return {"kind": normalized_kind, "project": project or default_project, "local_id": local_id}
    if len(parts) >= 3:
        project = parts[1].strip() or default_project
        local_id = ":".join(parts[2:]).strip()
        if local_id:
            return {"kind": kind, "project": project, "local_id": local_id}
    return None


async def build_simple_get_query_response(
    *,
    api_base: str,
    args: dict[str, Any],
    dependencies: SimpleReadDependencies,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    query = str(args.get("query") or args.get("question") or args.get("intent") or "").strip()
    if not query:
        return None

    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    limit = int(args.get("limit") or 10)
    state = str(args.get("state") or "planning")
    if not query_uses_project_expert(query, extract_task_id_like=dependencies.extract_task_id_like):
        results = await dependencies.post(
            api_base,
            "/memories/search",
            {
                "query": query,
                "project": project,
                "context_project": project,
                "limit": limit,
            },
        )
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Natural read query resolved through memory search.",
                "resource_kind": "memory_search",
                "count": len(results) if isinstance(results, list) else 0,
                "next_safe_action": "Use get with a returned memory ref for exact details, or refine the query.",
            },
            "result": compact_memory_search_results(results),
            "simple_interface": {"tool": "get", "mode": "query", "route": "memory_search"},
            "next_safe_action": "Use get with a returned memory ref for exact details, or refine the query.",
            "details_available": True,
        }

    expert_args = {
        "project": project,
        "question": query,
        "detail": str(args.get("detail") or "compact"),
        "limit": limit,
        "client_profile": str(args.get("client_profile") or "agent"),
        "response_format": _response_format(args),
    }
    data = await dependencies.query_project_expert(api_base, expert_args, session_id)
    return {
        "state": state,
        "project": project,
        "receipt": {
            "status": "accepted",
            "message": "Natural read query resolved through the project expert.",
            "resource_kind": "query",
            "next_safe_action": "Use get with a returned ref for more detail, or put only after reviewing a public form.",
        },
        "result": compact_project_expert_result(data, args),
        "simple_interface": {"tool": "get", "mode": "query"},
        "next_safe_action": "Use get with a returned ref for more detail, or put only after reviewing a public form.",
        "details_available": True,
    }


def query_uses_project_expert(query: str, *, extract_task_id_like: TaskIdDetector) -> bool:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    if extract_task_id_like(query):
        return True
    return any(term in text for term in _PROJECT_QUERY_TERMS)


def compact_memory_search_results(results: Any) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    if not isinstance(results, list):
        return compact
    for item in results[:10]:
        if not isinstance(item, dict):
            continue
        memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
        compact.append(
            {
                "id": memory.get("id"),
                "content": memory.get("content"),
                "memory_type": memory.get("memory_type"),
                "category": memory.get("category"),
                "project": memory.get("project"),
                "score": item.get("score"),
            }
        )
    return compact


def compact_project_expert_result(data: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if str(args.get("detail") or "compact").strip().lower() == "full" or bool(args.get("diagnostic", False)):
        return data
    route = data.get("selected_expert_route") if isinstance(data.get("selected_expert_route"), dict) else {}
    text = str(data.get("result_text") or "").strip()
    return {
        "status": data.get("status"),
        "question": data.get("question"),
        "selected_facade": route.get("facade"),
        "answer": text[:1200],
    }


def _response_format(args: dict[str, Any]) -> str:
    requested = str(args.get("response_format") or "auto").strip().lower()
    if requested in {"diagnostic", "answer"}:
        return requested
    return "json"


def _public_ref_envelope(
    *,
    args: dict[str, Any],
    project: str,
    normalized_ref: str,
    requested_ref: str,
    kind: str,
    receipt: dict[str, Any],
    result: Any = None,
) -> dict[str, Any]:
    safe_action = str(receipt.get("next_safe_action") or "")
    public_receipt = {
        **receipt,
        "data_ref": normalized_ref,
        "requested_ref": requested_ref,
    }
    packet = {
        "state": str(args.get("state") or "planning"),
        "project": project,
        "receipt": public_receipt,
        "next_safe_action": safe_action,
    }
    if result is not None:
        packet["result"] = result
    return packet


_PROJECT_QUERY_TERMS = (
    "task",
    "tasks",
    "active work",
    "open work",
    "priority",
    "continue",
    "readiness",
    "ready",
    "usable",
    "verify",
    "verification",
    "restart",
    "health",
    "rule",
    "rules",
    "law",
    "laws",
    "constraint",
    "constraints",
    "checkpoint",
    "claim",
    "lease",
    "work_token",
    "finish_task",
    "project_context",
    "\u0437\u0430\u0434\u0430\u0447",
    "\u0437\u0430\u0434\u0430\u0447\u0438",
    "\u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0435",
    "\u043f\u0440\u0438\u043e\u0440\u0438\u0442\u0435\u0442",
    "\u043f\u0440\u043e\u0434\u043e\u043b\u0436",
    "\u0433\u043e\u0442\u043e\u0432",
    "\u043f\u0440\u043e\u0432\u0435\u0440",
    "\u0432\u0435\u0440\u0438\u0444",
    "\u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u043a",
    "\u0437\u0434\u043e\u0440\u043e\u0432",
    "\u043f\u0440\u0430\u0432\u0438\u043b",
    "\u0437\u0430\u043a\u043e\u043d",
    "\u043e\u0433\u0440\u0430\u043d\u0438\u0447",
)
