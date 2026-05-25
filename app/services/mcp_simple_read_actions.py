from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.public_ref_index import (
    AmbiguousPublicRefError,
    PublicRefNotFoundError,
    canonical_artifact_key_for_short_ref,
    get_public_ref_index_store,
    is_short_public_id,
    public_artifact_matches_short_id,
)


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
    ref_source = ""
    try:
        if kind == "task":
            ref_source = "direct"
            if is_short_public_id(local_id):
                resolution = await resolve_public_artifact_short_ref(
                    api_base=api_base,
                    project=project,
                    artifact_type="task",
                    local_id=local_id,
                    dependencies=dependencies,
                )
                local_id = resolution["local_id"]
                normalized_ref = resolution["artifact_key"]
                ref_source = resolution["source"]
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
            if isinstance(result, dict):
                result.setdefault("artifact_key", normalized_ref)
                result.setdefault("public_ref_source", ref_source)
            next_safe_action = result.get("next_safe_action") or "Review task context before claiming or editing."
        elif kind in {"improvement", "artifact"}:
            artifact_key = address.get("artifact_key") or f"{kind}:{project}:{local_id}"
            result, artifact_key, ref_source = await get_artifact_by_public_ref(
                api_base=api_base,
                project=project,
                kind=address.get("artifact_type") or kind,
                local_id=local_id,
                artifact_key=artifact_key,
                dependencies=dependencies,
            )
            normalized_ref = artifact_key
            if isinstance(result, dict):
                result.setdefault("public_ref_source", ref_source)
            next_safe_action = "Review this read-only artifact before choosing any mutating mailbox form."
        elif kind == "law":
            ref_source = "direct"
            result = await dependencies.get(api_base, f"/laws/{quote(local_id, safe='')}")
            next_safe_action = "Review this read-only law before proposing revisions or candidates."
        elif kind == "rule_candidate":
            ref_source = "direct"
            result = await dependencies.get(api_base, f"/laws/candidates/{quote(local_id, safe='')}")
            next_safe_action = "Review this read-only rule candidate before any promotion, revision, or review action."
        elif kind == "memory":
            ref_source = "direct"
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
    except AmbiguousPublicRefError as exc:
        return _public_ref_envelope(
            args=args,
            project=project,
            normalized_ref=normalized_ref,
            requested_ref=requested_ref,
            kind=kind,
            receipt={
                "status": "ambiguous_ref",
                "message": "Public ref short id matched multiple artifacts.",
                "matches": compact_public_ref_matches(exc.matches),
                "next_safe_action": "Call get again with a longer id prefix or a full public ref from matches.",
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
            "ref_source": ref_source,
            "next_safe_action": next_safe_action,
        },
        result=result,
    )


async def get_artifact_by_public_ref(
    *,
    api_base: str,
    project: str,
    kind: str,
    local_id: str,
    artifact_key: str,
    dependencies: PublicRefDependencies,
) -> tuple[dict[str, Any], str, str]:
    try:
        result = await dependencies.get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
        if isinstance(result, dict):
            get_public_ref_index_store().upsert_artifact(result)
        return result, artifact_key, "direct"
    except Exception:
        if not is_short_public_id(local_id):
            raise
    resolution = await resolve_public_artifact_short_ref(
        api_base=api_base,
        project=project,
        artifact_type=str(kind or "").strip(),
        local_id=local_id,
        dependencies=dependencies,
    )
    resolved_key = resolution["artifact_key"]
    result = await dependencies.get(api_base, f"/artifacts/{quote(resolved_key, safe='')}")
    if isinstance(result, dict):
        get_public_ref_index_store().upsert_artifact(result)
    return result, resolved_key, resolution["source"]


async def resolve_public_artifact_short_ref(
    *,
    api_base: str,
    project: str,
    artifact_type: str,
    local_id: str,
    dependencies: PublicRefDependencies,
) -> dict[str, str]:
    store = get_public_ref_index_store()
    try:
        resolution = store.resolve(project=project, requested_type=artifact_type, short_id=local_id)
        try:
            await dependencies.get(api_base, f"/artifacts/{quote(resolution.artifact_key, safe='')}")
            return {
                "artifact_key": resolution.artifact_key,
                "local_id": resolution.local_id,
                "source": resolution.source,
            }
        except Exception:
            store.remove(resolution.artifact_key)
    except PublicRefNotFoundError:
        pass

    matches = await find_artifact_short_ref_matches(
        api_base=api_base,
        project=project,
        artifact_type=artifact_type,
        local_id=local_id,
        dependencies=dependencies,
    )
    if not matches and artifact_type in {"task", "improvement"}:
        matches = await find_artifact_short_ref_matches(
            api_base=api_base,
            project=project,
            artifact_type="all",
            local_id=local_id,
            dependencies=dependencies,
        )
    if len(matches) != 1:
        if matches:
            raise AmbiguousPublicRefError(matches)
        raise PublicRefNotFoundError("Public artifact short id did not resolve.")
    resolved_key = canonical_artifact_key_for_short_ref(
        matches[0],
        requested_type=str(artifact_type or "").strip(),
        short_id=local_id,
    )
    if not resolved_key:
        raise PublicRefNotFoundError("Public artifact short id resolved without an artifact key.")
    return {
        "artifact_key": resolved_key,
        "local_id": resolved_key.split(":", 2)[2],
        "source": "artifact_list_fallback",
    }


async def find_artifact_short_ref_matches(
    *,
    api_base: str,
    project: str,
    artifact_type: str,
    local_id: str,
    dependencies: PublicRefDependencies,
) -> list[dict[str, Any]]:
    type_query = "" if artifact_type == "all" else f"&type={quote(artifact_type, safe='')}"
    try:
        listed = await dependencies.get(
            api_base,
            f"/artifacts?project={quote(project, safe='')}{type_query}&limit=100",
        )
    except Exception:
        if artifact_type != "all":
            return []
        raise
    items = listed.get("items") if isinstance(listed, dict) else []
    get_public_ref_index_store().upsert_artifacts([item for item in items if isinstance(item, dict)])
    return [
        item
        for item in items
        if isinstance(item, dict)
        and public_artifact_matches_short_id(item, short_id=local_id)
    ]


def compact_public_ref_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in matches[:10]:
        compact.append(
            {
                "artifact_key": item.get("artifact_key"),
                "linked_artifact_key": item.get("linked_artifact_key"),
                "title": item.get("title"),
                "status": item.get("status"),
            }
        )
    return compact


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

    project = str(args.get("project") or args.get("project_id") or "").strip()
    limit = int(args.get("limit") or 10)
    state = str(args.get("state") or "planning")
    uses_project_expert = query_uses_project_expert(query, extract_task_id_like=dependencies.extract_task_id_like)
    if uses_project_expert and not project:
        return {
            "state": state,
            "project": "",
            "receipt": {
                "status": "needs_project",
                "message": "Project-scoped read query requires an explicit project or session project.",
                "resource_kind": "query",
                "missing_fields": ["project"],
                "next_safe_action": "Call get again with project set to the target project, or reconnect with a session project.",
            },
            "simple_interface": {"tool": "get", "mode": "query"},
            "next_safe_action": "Call get again with project set to the target project, or reconnect with a session project.",
            "details_available": True,
        }
    if not project:
        project = "mnemoforge"
    if not uses_project_expert:
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
    if explicit_memory_lookup(text):
        return False
    return any(term in text for term in _PROJECT_QUERY_TERMS)


def explicit_memory_lookup(text: str) -> bool:
    return any(
        marker in str(text or "")
        for marker in (
            "find memory",
            "find memories",
            "search memory",
            "search memories",
            "look up memory",
            "look up memories",
            "read memory",
            "read memories",
            "\u043d\u0430\u0439\u0434\u0438 \u043f\u0430\u043c\u044f\u0442",
            "\u043f\u043e\u0438\u0449\u0438 \u043f\u0430\u043c\u044f\u0442",
            "\u043f\u043e\u0438\u0441\u043a \u0432 \u043f\u0430\u043c\u044f\u0442",
        )
    )


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
    compact: dict[str, Any] = {
        "status": data.get("status"),
        "question": data.get("question"),
        "selected_facade": route.get("facade"),
    }
    parsed = parse_json_object(text)
    if parsed is not None:
        compact.update(compact_project_expert_payload(parsed, args))
    elif text:
        compact["answer"] = text[:1200]
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(str(text or "").strip())
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def compact_project_expert_payload(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 10)
    selected_route = payload.get("selected_route") if isinstance(payload.get("selected_route"), dict) else {}
    agent_action = payload.get("agent_action") if isinstance(payload.get("agent_action"), dict) else {}
    compact: dict[str, Any] = {
        "facade_status": payload.get("status"),
        "action_status": payload.get("action_status"),
        "executed": payload.get("executed"),
        "selected_route": compact_selected_route(selected_route),
        "summary": agent_action.get("one_sentence_summary"),
        "next_safe_action": payload.get("next_safe_action"),
    }
    items = project_expert_items(payload, limit=limit)
    if items is not None:
        compact["items"] = items
        compact["count"] = len(items)
    elif isinstance(payload.get("result"), dict):
        compact["data"] = compact_project_expert_data(payload["result"], limit=limit)
    elif isinstance(payload.get("result"), str):
        compact["answer"] = str(payload["result"])[:1200]
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def compact_selected_route(route: dict[str, Any]) -> dict[str, Any]:
    return {
        key: route.get(key)
        for key in ("tool", "family", "intent_type", "confidence", "reason")
        if route.get(key) not in (None, "", [])
    }


def project_expert_items(payload: dict[str, Any], *, limit: int) -> list[Any] | None:
    compact_result = payload.get("compact_result")
    if isinstance(compact_result, list):
        return compact_result[:limit]
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    items = result.get("items") if isinstance(result.get("items"), list) else None
    if items is not None:
        return items[:limit]
    return None


def compact_project_expert_data(result: dict[str, Any], *, limit: int) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("status", "project", "task_id", "title", "task_status", "next_safe_action"):
        if result.get(key) not in (None, "", []):
            compact[key] = result.get(key)
    for key in ("matched_rules", "rules", "laws", "forms"):
        value = result.get(key)
        if isinstance(value, list):
            compact[key] = value[:limit]
    return compact or {key: result.get(key) for key in list(result.keys())[:8]}


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
