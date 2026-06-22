from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.adherence_query_routing import adherence_query_next_action
from app.services.public_ref_index import (
    AmbiguousPublicRefError,
    PublicRefNotFoundError,
    canonical_artifact_key_for_short_ref,
    get_public_ref_index_store,
    is_short_public_id,
    public_artifact_matches_short_id,
)
from app.services.context_cue_service import (
    context_cues_for_query,
    context_cues_for_state,
    expand_context_cue,
    governed_law_to_cue,
)
from app.services.cognitive_health_service import build_cognitive_health_packet
from app.services.memory_store import get_memory_store
from app.services.context_page_store import get_context_page_store
from app.services.task_reconciliation_service import get_task_reconciliation_store
from app.services.planning_advisor_service import build_next_work_advisor, is_planning_advisor_query
from app.services.public_diagnostic_service import attach_public_diagnostic_incident
from app.services.route_pattern_store import get_route_pattern_store
from app.services.stage_applicability_service import stage_applicability_metadata
from app.services.mcp_workflow_specs import load_named_json_spec
from app.services.mcp_user_explanation_service import user_explanation_for_artifact


GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
ProjectExpertQueryCallback = Callable[[str, dict[str, Any], str | None], Awaitable[dict[str, Any]]]
TaskRefCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
PublicErrorCallback = Callable[[Exception], str]
TaskIdDetector = Callable[[str], Any]


@dataclass(frozen=True)
class SimpleReadDependencies:
    get: GetCallback
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
            result = annotate_explicit_ref_expansion(
                result,
                block_id="governed_law",
                state=str(args.get("state") or ""),
            )
            next_safe_action = "Use this expanded law as explicit recall context, then return to the current public workflow step."
        elif kind == "rule_candidate":
            ref_source = "direct"
            result = await dependencies.get(api_base, f"/laws/candidates/{quote(local_id, safe='')}")
            result = annotate_explicit_ref_expansion(
                result,
                block_id="rule_candidate",
                state=str(args.get("state") or ""),
            )
            next_safe_action = "Use this expanded rule candidate as explicit review context before promotion or revision."
        elif kind == "memory":
            ref_source = "direct"
            if is_short_public_id(local_id):
                memory_resolution = await resolve_public_memory_short_ref(project=project, local_id=local_id)
                local_id = memory_resolution["memory_id"]
                normalized_ref = f"memory:{project}:{local_id}"
                ref_source = memory_resolution["source"]
            result = await get_memory_by_public_id(
                api_base=api_base,
                project=project,
                memory_id=local_id,
                dependencies=dependencies,
            )
            next_safe_action = "Review this read-only memory before creating new facts or updates."
        elif kind == "context_page":
            ref_source = "direct"
            include_history = bool(args.get("include_history", False)) or bool(args.get("diagnostic", False))
            result = get_context_page_store().get_page(
                page_id=local_id,
                include_history=include_history,
            )
            if result is None:
                raise PublicRefNotFoundError("Context page ref did not resolve as an active page.")
            normalized_ref = str(result.get("page_ref") or normalized_ref)
            next_safe_action = "Use this context page as auxiliary read-only context, then return to the current workflow step."
        elif kind == "cue":
            ref_source = "direct"
            result = expand_context_cue(requested_ref, project=project, state=str(args.get("state") or ""))
            if result is None:
                raise PublicRefNotFoundError("Context cue ref did not resolve.")
            applicability = result.get("stage_applicability") if isinstance(result, dict) else {}
            if isinstance(applicability, dict) and applicability.get("state_known") and not applicability.get("allowed_in_state"):
                next_safe_action = "Treat this expanded cue as reference-only here; the current workflow stage does not normally surface it."
            else:
                next_safe_action = "Use this expanded cue as explicit recall context, then return to the current public workflow step."
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
                    "supported_ref_kinds": ["task", "improvement", "artifact", "law", "rule_candidate", "memory", "context_page", "cue"],
                    "next_safe_action": "Use mailbox_state for available forms, or ask_project/project_work for natural read-only lookup.",
                },
            )
    except AmbiguousPublicRefError as exc:
        next_safe_action = "Call get again with a longer id prefix or a full public ref from matches."
        receipt = {
            "status": "ambiguous_ref",
            "message": "Public ref short id matched multiple artifacts.",
            "matches": compact_public_ref_matches(exc.matches),
            "next_safe_action": next_safe_action,
        }
        return _public_ref_envelope(
            args=args,
            project=project,
            normalized_ref=normalized_ref,
            requested_ref=requested_ref,
            kind=kind,
            receipt=attach_public_diagnostic_incident(
                receipt=receipt,
                kind="ambiguous_public_ref",
                resource_kind=kind or "artifact",
            ),
        )
    except Exception as exc:
        next_safe_action = "Verify the public ref from the latest list/open/context result, then request mailbox_get again."
        receipt = {
            "status": "not_found",
            "message": dependencies.public_error_message(exc),
            "next_safe_action": next_safe_action,
        }
        diagnostic_allowed = bool(args.get("diagnostic")) or str(
            args.get("runtime_profile_id") or ""
        ).strip() == "diagnostic_operator"
        if diagnostic_allowed and kind in {"task", "improvement", "artifact"}:
            orphan = get_public_ref_index_store().find_exact(artifact_key=normalized_ref)
            if orphan:
                receipt.update(
                    status="orphan_ref",
                    message=(
                        "A non-authoritative historical public-ref index entry exists, "
                        "but the artifact is absent from authoritative SQLite storage."
                    ),
                    orphan_reference={
                        "artifact_key": orphan.get("artifact_key"),
                        "ref_kind": orphan.get("ref_kind"),
                        "project": orphan.get("project"),
                        "local_id": orphan.get("local_id"),
                        "title": orphan.get("title"),
                        "last_indexed_at": orphan.get("updated_at"),
                        "index_source": orphan.get("index_source"),
                        "data_root": orphan.get("data_root"),
                        "authoritative": False,
                    },
                    next_safe_action=(
                        "Inspect backups or an explicitly selected historical data root; "
                        "do not treat this index entry as a valid task and do not restore or delete automatically."
                    ),
                )
        return _public_ref_envelope(
            args=args,
            project=project,
            normalized_ref=normalized_ref,
            requested_ref=requested_ref,
            kind=kind,
            receipt=attach_public_diagnostic_incident(
                receipt=receipt,
                kind="public_ref_not_found",
                resource_kind=kind or "artifact",
            ),
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


def annotate_explicit_ref_expansion(result: Any, *, block_id: str, state: str = "") -> Any:
    if not isinstance(result, dict):
        return result
    annotated = dict(result)
    annotated.setdefault("expanded_by", "explicit_ref")
    applicability = stage_applicability_metadata(block_id, state=state)
    annotated.setdefault(
        "stage_applicability",
        {key: value for key, value in applicability.items() if value not in (None, "", [], {})},
    )
    return annotated


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
            pass
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


async def resolve_public_memory_short_ref(*, project: str, local_id: str) -> dict[str, str]:
    matches = await get_memory_store().find_by_id_prefix(local_id, project=project, limit=20)
    if len(matches) != 1:
        if matches:
            raise AmbiguousPublicRefError(compact_memory_ref_matches(matches))
        raise PublicRefNotFoundError("Public memory short id did not resolve.")
    return {"memory_id": str(matches[0].get("memory_id") or ""), "source": "memory_store_prefix"}


async def get_memory_by_public_id(
    *,
    api_base: str,
    project: str,
    memory_id: str,
    dependencies: PublicRefDependencies,
) -> dict[str, Any]:
    try:
        result = await dependencies.get(api_base, f"/memories/{quote(memory_id, safe='')}")
        return result if isinstance(result, dict) else {"id": memory_id, "value": result}
    except Exception:
        row = await get_memory_store().get(memory_id)
        if not row:
            raise
        return memory_store_row_to_public_memory(row, project=project)


def memory_store_row_to_public_memory(row: dict[str, Any], *, project: str) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {
        "id": row.get("memory_id"),
        "content": row.get("content"),
        "category": row.get("category"),
        "memory_type": metadata.get("memory_type"),
        "project": metadata.get("project") or project,
        "tags": metadata.get("tags") or [],
        "importance_score": metadata.get("importance_score"),
        "created_at": metadata.get("timestamp") or row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "source": metadata.get("source"),
    }


def compact_memory_ref_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for row in matches[:10]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        compact.append(
            {
                "ref": f"memory:{metadata.get('project') or ''}:{row.get('memory_id')}",
                "id": row.get("memory_id"),
                "category": row.get("category"),
                "memory_type": metadata.get("memory_type"),
                "project": metadata.get("project"),
                "content": str(row.get("content") or "")[:160],
            }
        )
    return compact


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
    if kind == "context_page" and len(parts) >= 2:
        page_id = ":".join(parts[1:]).strip()
        if page_id:
            return {"kind": "context_page", "project": default_project, "local_id": page_id, "artifact_key": f"context_page:{page_id}"}
    if kind == "cue" and len(parts) >= 2:
        cue_id = ":".join(parts[1:]).strip()
        if cue_id:
            return {"kind": "cue", "project": default_project, "local_id": cue_id}
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
    if explicit_project_alias_lookup(query):
        project_id = project or explicit_project_alias_project(query)
        suffix = f"?project_id={quote(project_id, safe='')}" if project_id else ""
        data = await dependencies.get(api_base, f"/project/identity/aliases{suffix}")
        return {
            "state": state,
            "project": project_id,
            "receipt": {
                "status": "accepted",
                "message": "Project identity aliases resolved through the public read surface.",
                "resource_kind": "project_aliases",
                "count": len(data.get("aliases") or []) if isinstance(data, dict) else 0,
                "next_safe_action": "Use these aliases as compatibility names; do not rewrite stored refs without rename_project/apply review.",
            },
            "result": compact_project_alias_results(data),
            "simple_interface": {"tool": "get", "mode": "query", "route": "project_aliases"},
            "next_safe_action": "Use these aliases as compatibility names; do not rewrite stored refs without rename_project/apply review.",
            "details_available": True,
        }
    reconciliation_ref = explicit_task_reconciliation_ref(query)
    if reconciliation_ref:
        packet = get_task_reconciliation_store().packet_for_target(reconciliation_ref)
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Task reconciliation packet resolved through the public read surface.",
                "resource_kind": "task_reconciliation",
                "data_ref": reconciliation_ref,
                "next_safe_action": packet.get("next_safe_action"),
            },
            "result": packet,
            "simple_interface": {"tool": "get", "mode": "query", "route": "task_reconciliation"},
            "next_safe_action": packet.get("next_safe_action"),
            "details_available": True,
        }

    spec_route = select_simple_get_spec_route(query)
    if spec_route:
        routed = await execute_simple_get_spec_route(
            api_base=api_base,
            args=args,
            query=query,
            project=project,
            limit=limit,
            state=state,
            route=spec_route,
            dependencies=dependencies,
        )
        if routed is not None:
            return routed
    learned_route = get_route_pattern_store().match(
        facade="get",
        pattern=query,
        allowed_intent_types={"artifact_lookup", "task_status_list"},
        semantic_threshold=0.82,
    )
    if learned_route and not bool(learned_route.get("mutating")):
        routed = await execute_learned_get_route(
            api_base=api_base,
            args=args,
            query=query,
            project=project,
            limit=limit,
            state=state,
            route=learned_route,
            dependencies=dependencies,
        )
        if routed is not None:
            return routed
    memory_lookup_id = explicit_memory_lookup_id(query)
    if memory_lookup_id:
        if not project:
            project = "mnemoforge"
        try:
            if is_short_public_id(memory_lookup_id):
                memory_resolution = await resolve_public_memory_short_ref(project=project, local_id=memory_lookup_id)
                memory_id = memory_resolution["memory_id"]
                ref_source = memory_resolution["source"]
            else:
                memory_id = memory_lookup_id
                ref_source = "direct"
            result = await get_memory_by_public_id(
                api_base=api_base,
                project=project,
                memory_id=memory_id,
                dependencies=PublicRefDependencies(
                    get=dependencies.get,
                    get_task_context=lambda _api_base, _args: {},
                    public_error_message=lambda exc: str(exc),
                ),
            )
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "accepted",
                    "message": "Explicit memory id query resolved through memory lookup.",
                    "resource_kind": "memory",
                    "ref_source": ref_source,
                    "data_ref": f"memory:{project}:{memory_id}",
                    "requested_ref": query,
                    "next_safe_action": "Review this read-only memory before creating new facts or updates.",
                },
                "result": result,
                "simple_interface": {"tool": "get", "mode": "query", "route": "memory_ref_lookup"},
                "next_safe_action": "Review this read-only memory before creating new facts or updates.",
                "details_available": True,
            }
        except AmbiguousPublicRefError as exc:
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "ambiguous_ref",
                    "message": "Public memory short id matched multiple memories.",
                    "matches": exc.matches,
                    "next_safe_action": "Call get again with a longer memory id prefix or a full memory ref from matches.",
                },
                "next_safe_action": "Call get again with a longer memory id prefix or a full memory ref from matches.",
                "details_available": True,
            }
        except Exception as exc:
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "not_found",
                    "message": str(exc),
                    "resource_kind": "memory",
                    "next_safe_action": "Verify the memory id or search memories with a descriptive query.",
                },
                "next_safe_action": "Verify the memory id or search memories with a descriptive query.",
                "details_available": True,
            }

    artifact_list_type = explicit_artifact_list_type(query)
    if artifact_list_type and not project:
        next_safe_action = "Call get again with project set to the target project."
        receipt = {
            "status": "needs_project",
            "message": "Project-scoped artifact list query requires an explicit project or session project.",
            "resource_kind": "artifact_list",
            "missing_fields": ["project"],
            "next_safe_action": next_safe_action,
        }
        return {
            "state": state,
            "project": "",
            "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
            "simple_interface": {"tool": "get", "mode": "query", "route": "artifact_list"},
            "next_safe_action": next_safe_action,
            "details_available": True,
        }
    if artifact_list_type and project:
        status = artifact_list_status_filter(query)
        type_query = "" if artifact_list_type == "all" else f"&type={quote(artifact_list_type, safe='')}"
        topic_query = artifact_topic_query(query, artifact_list_type)
        status_query = f"&status={quote(status, safe='')}" if status else ""
        search_query = f"&query={quote(topic_query, safe='')}" if topic_query else ""
        data = await dependencies.get(
            api_base,
            f"/artifacts?project={quote(project, safe='')}{status_query}&limit={limit}{type_query}{search_query}",
        )
        if not isinstance(data, dict):
            data = await dependencies.query_project_expert(
                api_base,
                {
                    "project": project,
                    "question": query,
                    "detail": str(args.get("detail") or "compact"),
                    "limit": limit,
                    "client_profile": str(args.get("client_profile") or "agent"),
                    "response_format": _response_format(args),
                    "artifact_type": artifact_list_type,
                },
                session_id,
            )
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "accepted",
                    "message": "Natural artifact list query resolved through the project expert fallback.",
                    "resource_kind": "artifact_list",
                    "next_safe_action": "Use get with a returned ref for more detail.",
                },
                "result": compact_project_expert_result(data, args),
                "simple_interface": {"tool": "get", "mode": "query", "route": "artifact_list_fallback"},
                "next_safe_action": "Use get with a returned ref for more detail.",
                "details_available": True,
            }
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Natural artifact list query resolved through artifact search.",
                "resource_kind": "artifact_list",
                "artifact_type": artifact_list_type,
                "status_filter": status,
                "count": len(data.get("items") or []) if isinstance(data.get("items"), list) else 0,
                "next_safe_action": "Use get with a returned artifact ref for more detail.",
            },
            "result": compact_artifact_list_results(data, limit=limit),
            "simple_interface": {"tool": "get", "mode": "query", "route": "artifact_list"},
            "next_safe_action": "Use get with a returned artifact ref for more detail.",
            "details_available": True,
        }
    if is_planning_advisor_query(query):
        if not project:
            next_safe_action = "Call get again with project set to the target project."
            receipt = {
                "status": "needs_project",
                "message": "Planning advisor queries require an explicit project or session project.",
                "resource_kind": "planning_advisor",
                "missing_fields": ["project"],
                "next_safe_action": next_safe_action,
            }
            return {
                "state": state,
                "project": "",
                "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
                "simple_interface": {"tool": "get", "mode": "query", "route": "planning_advisor"},
                "next_safe_action": next_safe_action,
                "details_available": True,
            }
        data = await dependencies.get(
            api_base,
            f"/artifacts?project={quote(project, safe='')}&status=open&limit={limit}",
        )
        advisor = build_next_work_advisor(data if isinstance(data, dict) else {}, project=project, query=query, limit=limit)
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Next-work query resolved through the planning advisor.",
                "resource_kind": "planning_advisor",
                "count": len(advisor.get("next_work_candidates") or []),
                "next_safe_action": advisor.get("next_safe_action"),
            },
            "result": advisor,
            "simple_interface": {"tool": "get", "mode": "query", "route": "planning_advisor"},
            "next_safe_action": advisor.get("next_safe_action"),
            "details_available": True,
        }
    uses_project_expert = query_uses_project_expert(query, extract_task_id_like=dependencies.extract_task_id_like)
    if uses_project_expert and not project:
        next_safe_action = "Call get again with project set to the target project, or reconnect with a session project."
        receipt = {
            "status": "needs_project",
            "message": "Project-scoped read query requires an explicit project or session project.",
            "resource_kind": "query",
            "missing_fields": ["project"],
            "next_safe_action": next_safe_action,
        }
        return {
            "state": state,
            "project": "",
            "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
            "simple_interface": {"tool": "get", "mode": "query"},
            "next_safe_action": next_safe_action,
            "details_available": True,
        }
    if not project:
        project = "mnemoforge"
    if not uses_project_expert and terse_topic_artifact_query(query):
        data = await dependencies.get(
            api_base,
            f"/artifacts?project={quote(project, safe='')}&query={quote(query, safe='')}&limit={limit}",
        )
        if isinstance(data, dict) and data.get("items"):
            return {
                "state": state,
                "project": project,
                "receipt": {
                    "status": "accepted",
                    "message": "Terse topic query resolved through artifact search.",
                    "resource_kind": "artifact_list",
                    "count": len(data.get("items") or []),
                    "next_safe_action": "Use get with a returned artifact ref for more detail, or refine the query.",
                },
                "result": compact_artifact_list_results(data, limit=limit),
                "simple_interface": {"tool": "get", "mode": "query", "route": "artifact_topic_lookup"},
                "next_safe_action": "Use get with a returned artifact ref for more detail, or refine the query.",
                "details_available": True,
            }
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
    if explicit_memory_lookup(text):
        return False
    if extract_task_id_like(query):
        return True
    return any(term in text for term in _PROJECT_QUERY_TERMS)


def explicit_artifact_list_type(query: str) -> str:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    if not any(term in text for term in _READ_LOOKUP_TERMS):
        return ""
    if any(term in text for term in ("open tasks", "active tasks", "open work", "active work", "list open", "list active")):
        return ""
    asks_improvements = any(term in text for term in _IMPROVEMENT_QUERY_TERMS)
    asks_tasks = any(term in text for term in _TASK_QUERY_TERMS)
    asks_work_items = any(term in text for term in ("work items", "work item", "artifacts", "artifact"))
    if asks_improvements and asks_tasks:
        return "all"
    if asks_improvements:
        return "improvement"
    if asks_tasks:
        return "task"
    if asks_work_items:
        return "all"
    return ""


def terse_topic_artifact_query(query: str) -> bool:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold().strip()
    if not text or explicit_memory_lookup(text):
        return False
    if any(term in text for term in ("memory", "memories", "fact", "facts", "stored", "context")):
        return False
    if any(term in text for term in _PROJECT_QUERY_TERMS):
        return False
    if any(term in text for term in ("create", "record", "save", "write", "delete", "close", "finish")):
        return False
    words = text.split()
    return 1 <= len(words) <= 5


def artifact_topic_query(query: str, artifact_list_type: str) -> str:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    stop_terms = {
        "find",
        "search",
        "show",
        "list",
        "lookup",
        "look",
        "up",
        "read",
        "get",
        "details",
        "detail",
        "about",
        "all",
        "and",
        "for",
        "with",
        "by",
        "task",
        "tasks",
        "improvement",
        "improvements",
        "work",
        "item",
        "items",
        "artifact",
        "artifacts",
        "open",
        "active",
        "unresolved",
        "pending",
        "backlog",
        "latest",
        "recent",
        "done",
        "closed",
        "resolved",
        "archived",
    }
    stop_terms.update(str(term).casefold() for term in _READ_LOOKUP_TERMS)
    stop_terms.update(str(term).casefold() for term in _TASK_QUERY_TERMS)
    stop_terms.update(str(term).casefold() for term in _IMPROVEMENT_QUERY_TERMS)
    stop_terms.update(str(term).casefold() for term in _PROJECT_QUERY_TERMS)
    spec = _artifact_lookup_spec()
    stop_terms.update(str(term).casefold() for term in spec.get("stop_terms") or [])
    stop_terms.update(str(term).casefold() for term in spec.get("structural_stop_terms") or [])
    status_aliases = spec.get("status_aliases")
    if isinstance(status_aliases, dict):
        for aliases in status_aliases.values():
            if not isinstance(aliases, list):
                continue
            for alias in aliases:
                stop_terms.update(str(alias or "").casefold().split())
    words = [
        word
        for word in text.split()
        if word not in stop_terms
        and not _word_matches_alias(word, stop_terms)
        and not _word_matches_status_alias(word)
    ]
    if not words or " ".join(words) == artifact_list_type:
        return ""
    return " ".join(words)


def _word_matches_alias(word: str, aliases: set[str]) -> bool:
    text = str(word or "").casefold()
    return any(_query_contains_alias(text, alias) for alias in aliases)


def _word_matches_status_alias(word: str) -> bool:
    status_aliases = _artifact_lookup_spec().get("status_aliases")
    if not isinstance(status_aliases, dict):
        return False
    text = str(word or "").casefold()
    return any(
        _query_contains_alias(text, str(alias))
        for aliases in status_aliases.values()
        if isinstance(aliases, list)
        for alias in aliases
    )


def artifact_list_status_filter(query: str) -> str:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    status_aliases = _artifact_lookup_spec().get("status_aliases")
    if not isinstance(status_aliases, dict):
        status_aliases = {}
    for status in ("open", "done", "paused", "archived"):
        aliases = status_aliases.get(status)
        if isinstance(aliases, list) and any(_query_contains_alias(text, str(alias)) for alias in aliases):
            return status
    return ""


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


def explicit_memory_lookup_id(query: str) -> str:
    text = str(query or "")
    normalized = re.sub(r"[_\-/\.]+", " ", text).casefold()
    if not explicit_memory_lookup(normalized):
        return ""
    uuid_match = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        text,
    )
    if uuid_match:
        return uuid_match.group(0)
    short_match = re.search(r"\b[0-9a-fA-F]{6,12}\b", text)
    return short_match.group(0) if short_match else ""


def explicit_task_reconciliation_ref(query: str) -> str:
    text = str(query or "").strip()
    lowered = text.casefold()
    if "reconciliation" not in lowered and "superseded" not in lowered:
        return ""
    match = re.search(r"task:[^\s,;]+:[^\s,;]+", text)
    return match.group(0) if match else ""


def explicit_project_alias_lookup(query: str) -> bool:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    return (
        "alias" in text
        or "aliases" in text
        or "project aliases" in text
        or "project name" in text
        or "project identity" in text
    ) and any(term in text for term in _READ_LOOKUP_TERMS)


def explicit_project_alias_project(query: str) -> str:
    text = str(query or "")
    match = re.search(r"\b(?:for|of)\s+([A-Za-z0-9_.-]{2,128})\b", text, flags=re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1).strip(" .,:;")
    stop_values = {"project", "aliases", "alias", "identity", "name", "names"}
    return "" if value.casefold() in stop_values else value


def _simple_get_route_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("search/simple_get_routes.json")
    except Exception:
        return {}


def select_simple_get_spec_route(query: str) -> dict[str, Any]:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    matches: list[tuple[float, int, dict[str, Any]]] = []
    for route in _simple_get_route_spec().get("routes") or []:
        if not isinstance(route, dict):
            continue
        for term in route.get("trigger_terms") or []:
            value = str(term or "").strip().casefold()
            if value and _query_contains_alias(text, value):
                selected = dict(route)
                selected["matched_trigger"] = value
                try:
                    priority = float(selected.get("priority") or 0.0)
                except Exception:
                    priority = 0.0
                matches.append((priority, len(value), selected))
                break
    if not matches:
        return {}
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = dict(matches[0][2])
    selected["route_conflicts"] = [
        {
            "id": str(item[2].get("id") or ""),
            "resource_kind": str(item[2].get("resource_kind") or ""),
            "priority": item[0],
            "matched_trigger": str(item[2].get("matched_trigger") or ""),
        }
        for item in matches[1:4]
    ]
    return selected


async def execute_simple_get_spec_route(
    *,
    api_base: str,
    args: dict[str, Any],
    query: str,
    project: str,
    limit: int,
    state: str,
    route: dict[str, Any],
    dependencies: SimpleReadDependencies,
) -> dict[str, Any] | None:
    route_id = str(route.get("id") or "").strip()
    resource_kind = str(route.get("resource_kind") or route_id).strip()
    matched_trigger = str(route.get("matched_trigger") or "").strip()
    simple_interface = {
        "tool": "get",
        "mode": "query",
        "route": route_id,
        "route_source": "simple_get_routes",
        "matched_trigger": matched_trigger,
    }
    simple_interface = {key: value for key, value in simple_interface.items() if value not in (None, "", [], {})}

    if resource_kind == "storage_trust":
        endpoint = str(route.get("endpoint") or "/admin/storage-trust").strip() or "/admin/storage-trust"
        storage_path = endpoint
        if project:
            separator = "&" if "?" in storage_path else "?"
            storage_path = f"{storage_path}{separator}project={quote(project, safe='')}"
        data = await dependencies.get(api_base, storage_path)
        result = data if isinstance(data, dict) else {}
        if not _full_detail_requested(args):
            result = compact_storage_trust_query_result(result)
        next_safe_action = (
            "Review storage trust warnings as system maintenance context; "
            "do not treat hygiene cleanup as current-project work unless explicitly promoted."
        )
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Natural read query resolved through storage trust.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "next_safe_action": next_safe_action,
            },
            "result": result,
            "simple_interface": simple_interface,
            "next_safe_action": next_safe_action,
            "details_available": True,
        }

    if resource_kind == "context_cues":
        if not project:
            next_safe_action = adherence_query_next_action(has_project=False)
            receipt = {
                "status": "needs_project",
                "message": "Adherence cue queries require an explicit project or session project.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "missing_fields": ["project"],
                "next_safe_action": next_safe_action,
            }
            return {
                "state": state,
                "project": "",
                "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
                "simple_interface": simple_interface,
                "next_safe_action": next_safe_action,
                "details_available": True,
            }
        cues = context_cues_for_query(query=query, project=project, state=state, max_cues=limit)
        if not cues:
            cues = context_cues_for_state(state=state, project=project, max_cues=limit)
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Natural adherence query resolved through context cues.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "count": len(cues),
                "next_safe_action": adherence_query_next_action(has_project=True),
            },
            "result": {
                "status": "ok",
                "query": query,
                "context_cues": cues,
                "no_verification_execution": True,
            },
            "simple_interface": simple_interface,
            "next_safe_action": adherence_query_next_action(has_project=True),
            "details_available": True,
        }

    if resource_kind == "boundary_action_cues":
        if not project:
            next_safe_action = "Call get again with project set so boundary-action cues can resolve project-scoped laws."
            receipt = {
                "status": "needs_project",
                "message": "Boundary-action cue queries require an explicit project or session project.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "missing_fields": ["project"],
                "next_safe_action": next_safe_action,
            }
            return {
                "state": state,
                "project": "",
                "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
                "simple_interface": simple_interface,
                "next_safe_action": next_safe_action,
                "details_available": True,
            }
        packet = await build_boundary_action_cue_packet(
            api_base=api_base,
            project=project,
            query=query,
            state=state,
            route=route,
            limit=limit,
            dependencies=dependencies,
        )
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Natural read query resolved through boundary-action governed law cues.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "action_class": packet.get("action_class"),
                "count": len(packet.get("context_cues") or []),
                "next_safe_action": packet.get("next_safe_action"),
            },
            "result": packet,
            "simple_interface": simple_interface,
            "next_safe_action": packet.get("next_safe_action"),
            "details_available": True,
        }

    if resource_kind == "live_runtime_preflight":
        if not project:
            next_safe_action = "Call get again with project set so live-runtime preflight can resolve project-scoped policy cues."
            receipt = {
                "status": "needs_project",
                "message": "Live-runtime preflight requires an explicit project or session project.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "missing_fields": ["project"],
                "next_safe_action": next_safe_action,
            }
            return {
                "state": state,
                "project": "",
                "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
                "simple_interface": simple_interface,
                "next_safe_action": next_safe_action,
                "details_available": True,
            }
        packet = build_live_runtime_preflight_packet(project=project, query=query, state=state, limit=limit)
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Natural read query resolved through live-runtime preflight.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "count": len(packet.get("context_cues") or []),
                "next_safe_action": packet.get("next_safe_action"),
            },
            "result": packet,
            "simple_interface": simple_interface,
            "next_safe_action": packet.get("next_safe_action"),
            "details_available": True,
        }

    if resource_kind == "cognitive_health":
        if not project:
            next_safe_action = "Call get again with project set so cognitive health can check project-scoped workflow recall."
            receipt = {
                "status": "needs_project",
                "message": "Cognitive health checks require an explicit project or session project.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "missing_fields": ["project"],
                "next_safe_action": next_safe_action,
            }
            return {
                "state": state,
                "project": "",
                "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
                "simple_interface": simple_interface,
                "next_safe_action": next_safe_action,
                "details_available": True,
            }
        packet = build_cognitive_health_packet(project=project, state=state, query=query, limit=limit)
        return {
            "state": state,
            "project": project,
            "receipt": {
                "status": "accepted",
                "message": "Natural read query resolved through cognitive health self-check.",
                "resource_kind": resource_kind,
                "route_source": "simple_get_routes",
                "matched_trigger": matched_trigger,
                "count": len(packet.get("checks") or []),
                "next_safe_action": packet.get("next_safe_action"),
            },
            "result": packet,
            "simple_interface": simple_interface,
            "next_safe_action": packet.get("next_safe_action"),
            "details_available": True,
        }

    return None


async def execute_learned_get_route(
    *,
    api_base: str,
    args: dict[str, Any],
    query: str,
    project: str,
    limit: int,
    state: str,
    route: dict[str, Any],
    dependencies: SimpleReadDependencies,
) -> dict[str, Any] | None:
    tool = str(route.get("tool") or "").strip()
    intent_type = str(route.get("intent_type") or "").strip()
    if tool not in {"project_context", "list_artifacts"} or intent_type not in {"artifact_lookup", "task_status_list"}:
        return None
    if not project:
        next_safe_action = "Call get again with project set to the target project."
        receipt = {
            "status": "needs_project",
            "message": "Learned project-scoped read route requires an explicit project or session project.",
            "resource_kind": "artifact_list",
            "route_source": "learned_route_pattern",
            "missing_fields": ["project"],
            "next_safe_action": next_safe_action,
        }
        return {
            "state": state,
            "project": "",
            "receipt": attach_public_diagnostic_incident(receipt=receipt, kind="missing_project_scope"),
            "simple_interface": {"tool": "get", "mode": "query", "route": "learned_route_pattern"},
            "next_safe_action": next_safe_action,
            "details_available": True,
        }

    payload = learned_route_payload(route)
    artifact_type = str(payload.get("type") or ("task" if intent_type == "task_status_list" else "all")).strip() or "all"
    status = str(payload.get("status") or "").strip()
    include_query = bool(payload.get("include_query", True))
    type_query = "" if artifact_type == "all" else f"&type={quote(artifact_type, safe='')}"
    status_query = f"&status={quote(status, safe='')}" if status else ""
    search_query = f"&query={quote(query, safe='')}" if include_query else ""
    data = await dependencies.get(
        api_base,
        f"/artifacts?project={quote(project, safe='')}{status_query}&limit={limit}{type_query}{search_query}",
    )
    if not isinstance(data, dict):
        return None
    return {
        "state": state,
        "project": project,
        "receipt": {
            "status": "accepted",
            "message": "Natural read query resolved through a learned route pattern.",
            "resource_kind": "artifact_list",
            "route_source": "learned_route_pattern",
            "pattern_id": route.get("pattern_id"),
            "matched_by": route.get("matched_by"),
            "artifact_type": artifact_type,
            "status_filter": status,
            "count": len(data.get("items") or []) if isinstance(data.get("items"), list) else 0,
            "next_safe_action": "Use get with a returned artifact ref for more detail.",
        },
        "result": compact_artifact_list_results(data, limit=limit),
        "simple_interface": {
            "tool": "get",
            "mode": "query",
            "route": "learned_route_pattern",
            "intent_type": intent_type,
        },
        "next_safe_action": "Use get with a returned artifact ref for more detail.",
        "details_available": True,
    }


def learned_route_payload(route: dict[str, Any]) -> dict[str, Any]:
    metadata = route.get("metadata") if isinstance(route.get("metadata"), dict) else {}
    learned = metadata.get("learned_payload")
    if isinstance(learned, dict) and learned:
        return dict(learned)
    events = metadata.get("feedback_events")
    if isinstance(events, list):
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            context = event.get("context")
            if not isinstance(context, dict):
                continue
            expected = context.get("expected_payload")
            if isinstance(expected, dict):
                return dict(expected)
    return {}


async def build_boundary_action_cue_packet(
    *,
    api_base: str,
    project: str,
    query: str,
    state: str,
    route: dict[str, Any],
    limit: int,
    dependencies: SimpleReadDependencies,
) -> dict[str, Any]:
    try:
        spec = load_named_json_spec("workflow/boundary_action_cues.json")
    except Exception:
        spec = {}
    action_classes = spec.get("action_classes") if isinstance(spec.get("action_classes"), dict) else {}
    action_class = select_boundary_action_class(query=query, route=route, spec=spec)
    action_spec = action_classes.get(action_class) if isinstance(action_classes.get(action_class), dict) else {}
    laws = await read_active_project_laws(api_base=api_base, project=project, dependencies=dependencies)
    cues = boundary_action_law_cues(
        laws,
        project=project,
        query=query,
        action_class=action_class,
        action_spec=action_spec,
        limit=max(1, min(int(limit or spec.get("default_limit") or 5), 10)),
    )
    next_safe_action = str(spec.get("next_safe_action") or "").strip() or (
        "Review compact governed-law cues for this boundary action; expand only refs that are needed before proceeding."
    )
    return {
        "status": "ok" if cues else "no_matching_governed_law_cues",
        "project": project,
        "query": query,
        "state": state,
        "action_class": action_class,
        "action_title": action_spec.get("title") or action_class,
        "read_only": True,
        "context_cues": cues,
        "expand_only_if_needed": True,
        "next_safe_action": next_safe_action,
    }


def select_boundary_action_class(*, query: str, route: dict[str, Any], spec: dict[str, Any]) -> str:
    text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    classes = spec.get("action_classes") if isinstance(spec.get("action_classes"), dict) else {}
    best: tuple[int, str] | None = None
    for class_id, item in classes.items():
        if not isinstance(item, dict):
            continue
        score = 0
        for term in item.get("trigger_terms") or []:
            value = str(term or "").strip().casefold()
            if value and _query_contains_alias(text, value):
                score += max(1, len(value.split()))
        if score and (best is None or score > best[0]):
            best = (score, str(class_id))
    if best:
        return best[1]
    return str(spec.get("default_action_class") or route.get("id") or "boundary_action").strip() or "boundary_action"


async def read_active_project_laws(
    *,
    api_base: str,
    project: str,
    dependencies: SimpleReadDependencies,
) -> list[dict[str, Any]]:
    try:
        data = await dependencies.get(
            api_base,
            f"/laws?project={quote(project, safe='')}&status=active&include_promoted=true&limit=100",
        )
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


def boundary_action_law_cues(
    laws: list[dict[str, Any]],
    *,
    project: str,
    query: str,
    action_class: str,
    action_spec: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    query_text = re.sub(r"[_\-/\.]+", " ", str(query or "")).casefold()
    action_terms = [
        str(term or "").casefold()
        for term in action_spec.get("trigger_terms") or []
        if str(term or "").strip()
    ]
    action_terms.extend(str(action_class or "").replace("_", " ").casefold().split())
    scored: list[tuple[int, dict[str, Any]]] = []
    for law in laws:
        cue = governed_law_to_cue(law, current_project=project)
        if not cue:
            continue
        haystack = " ".join(
            str(value or "")
            for value in (
                law.get("title"),
                law.get("statement"),
                law.get("rationale"),
                law.get("topic_path"),
                " ".join(str(tag) for tag in (law.get("tags") or [])),
            )
        ).casefold()
        score = 0
        for term in action_terms:
            if term and term in haystack:
                score += max(1, len(term.split()))
        for token in query_text.split():
            if len(token) >= 4 and token in haystack:
                score += 1
        if score:
            compact = {
                key: value
                for key, value in cue.items()
                if key not in {"full_text", "scope", "tags", "trigger_terms"} and value not in (None, "", [], {})
            }
            compact["reason"] = f"action_boundary:{action_class}"
            scored.append((score, compact))
    scored.sort(key=lambda item: (item[0], str(item[1].get("severity") or "")), reverse=True)
    return [cue for _, cue in scored[:limit]]


def build_live_runtime_preflight_packet(*, project: str, query: str, state: str = "", limit: int = 5) -> dict[str, Any]:
    try:
        spec = load_named_json_spec("workflow/live_runtime_preflight.json")
    except Exception:
        spec = {}
    preflight_state = str(state or spec.get("default_state") or "live_validation").strip() or "live_validation"
    cue_limit = max(1, min(int(limit or 5), 10))
    cues = [
        *context_cues_for_query(query=query, project=project, state=preflight_state, max_cues=cue_limit),
        *context_cues_for_state(state=preflight_state, project=project, max_cues=cue_limit),
    ]
    compact_cues: list[dict[str, Any]] = []
    seen_cues: set[str] = set()
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        cue_id = str(cue.get("cue") or "").strip()
        if not cue_id or cue_id in seen_cues:
            continue
        seen_cues.add(cue_id)
        compact_cues.append({key: value for key, value in cue.items() if key != "full_text" and value not in (None, "", [], {})})
        if len(compact_cues) >= cue_limit:
            break
    checks = []
    for item in spec.get("checks") or []:
        if not isinstance(item, dict):
            continue
        checks.append(
            {
                key: item.get(key)
                for key in ("id", "severity", "question", "why")
                if item.get(key) not in (None, "", [], {})
            }
        )
    next_safe_action = str(spec.get("next_safe_action") or "").strip() or (
        "Review preflight cues before live-runtime action; expand refs only when recall is insufficient."
    )
    return {
        "status": "needs_preflight_review",
        "project": project,
        "query": query,
        "preflight_state": preflight_state,
        "read_only": True,
        "no_live_action_executed": True,
        "context_cues": compact_cues,
        "checks": checks,
        "expand_only_if_needed": True,
        "next_safe_action": next_safe_action,
    }


def _artifact_lookup_spec() -> dict[str, Any]:
    try:
        return load_named_json_spec("search/artifact_lookup.json")
    except Exception:
        return {}


def _query_contains_alias(text: str, alias: str) -> bool:
    value = str(alias or "").strip().casefold()
    if not value:
        return False
    if re.fullmatch(r"[\w]+", value, flags=re.UNICODE):
        return re.search(rf"\b{re.escape(value)}", text, flags=re.UNICODE) is not None
    return value in text


def explicit_storage_trust_query(query: str) -> bool:
    return str(select_simple_get_spec_route(query).get("id") or "") == "storage_trust"


def compact_storage_trust_query_result(result: dict[str, Any]) -> dict[str, Any]:
    signals = result.get("signals") if isinstance(result.get("signals"), dict) else {}
    hygiene = result.get("data_hygiene") if isinstance(result.get("data_hygiene"), dict) else {}
    maintenance = result.get("maintenance_suggestion") if isinstance(result.get("maintenance_suggestion"), dict) else {}
    playbook = result.get("playbook") if isinstance(result.get("playbook"), dict) else {}
    workflow = playbook.get("workflow") if isinstance(playbook.get("workflow"), dict) else {}
    scope_summary = hygiene.get("scope_summary") if isinstance(hygiene.get("scope_summary"), dict) else {}
    workflow_scope = workflow.get("scope_summary") if isinstance(workflow.get("scope_summary"), dict) else {}
    compact = {
        "status": result.get("status"),
        "summary": result.get("summary"),
        "maintenance_suggestion": maintenance or None,
        "signals": {
            key: signals.get(key)
            for key in (
                "degraded_slices",
                "active_hygiene_findings",
                "manual_review_pending",
                "quarantine_candidates",
                "delete_ready",
            )
            if signals.get(key) not in (None, "", [], {})
        },
        "data_hygiene": {
            key: hygiene.get(key)
            for key in ("status", "active_findings")
            if hygiene.get(key) not in (None, "", [], {})
        },
        "workflow_scope": None if maintenance else (workflow_scope or scope_summary),
        "next_actions": list(result.get("next_actions") or [])[:5],
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def compact_project_alias_results(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"aliases": []}
    aliases = data.get("aliases") if isinstance(data.get("aliases"), list) else []
    compact_aliases = []
    for item in aliases[:50]:
        if not isinstance(item, dict):
            continue
        compact_aliases.append(
            {
                key: item.get(key)
                for key in ("alias", "project_id", "status", "reason", "effective_from", "effective_to")
                if item.get(key) not in (None, "", [])
            }
        )
    return {
        "aliases": compact_aliases,
        "count": len(compact_aliases),
    }


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


def compact_artifact_list_results(data: dict[str, Any], *, limit: int) -> dict[str, Any]:
    items = data.get("items") if isinstance(data.get("items"), list) else []
    compact_items: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compact = {
            "artifact_key": item.get("artifact_key"),
            "type": item.get("type"),
            "id": item.get("id") or item.get("task_id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "task_id": item.get("task_id"),
            "linked_artifact_key": item.get("linked_artifact_key"),
            "linked_status": item.get("linked_status"),
            "match_reason": item.get("match_reason"),
            "user_explanation": user_explanation_for_artifact(item),
            "matched_topic_tags": item.get("matched_topic_tags"),
        }
        compact_items.append({key: value for key, value in compact.items() if value not in (None, "", [])})
    return {
        "items": compact_items,
        "count": len(compact_items),
    }


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


def _full_detail_requested(args: dict[str, Any]) -> bool:
    return str(args.get("detail") or "compact").strip().lower() == "full" or bool(args.get("diagnostic", False))


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


_IMPROVEMENT_QUERY_TERMS = (
    "improvement",
    "improvements",
    "backlog",
    "idea",
    "ideas",
    "proposal",
    "proposals",
    "\u0443\u043b\u0443\u0447\u0448",
    "\u0438\u0434\u0435\u0438",
    "\u0431\u044d\u043a\u043b\u043e\u0433",
)


_TASK_QUERY_TERMS = (
    "task",
    "tasks",
    "\u0437\u0430\u0434\u0430\u0447",
    "\u0437\u0430\u0434\u0430\u0447\u0438",
)
