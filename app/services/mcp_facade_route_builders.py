"""Route builders for project_context, project_verify, and project_capture facades."""
from __future__ import annotations

from typing import Any

from app.services.mcp_facade_backend_routing import facade_backend_requested as _facade_backend_requested
from app.services.mcp_simple_read_actions import (
    artifact_list_status_filter,
    artifact_topic_query,
    explicit_artifact_list_type,
)
from app.services.mcp_sse_tool_catalog import (
    _catalog_route_by_intent,
    _extract_task_id_like_from_text,
    _is_full_uuid,
    _learned_payload_from_decision,
    _render_route_payload_template,
    _selected_catalog_route,
)
from app.services.mcp_workflow_specs import load_route_catalog_spec


def _string_list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []

PROJECT_CONTEXT_ROUTE_CATALOG: tuple[dict[str, Any], ...] = tuple(
    route.model_dump()
    for route in load_route_catalog_spec("project_context").routes
)


PROJECT_VERIFY_ROUTE_CATALOG: tuple[dict[str, Any], ...] = tuple(
    route.model_dump()
    for route in load_route_catalog_spec("project_verify").routes
)


PROJECT_CAPTURE_ROUTE_CATALOG: tuple[dict[str, Any], ...] = tuple(
    route.model_dump()
    for route in load_route_catalog_spec("project_capture").routes
)




def project_context_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = str(args.get("intent") or args.get("task") or "").strip()
    text = intent.casefold()
    project = str(args.get("project_id") or args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args.get("task_id") or _extract_task_id_like_from_text(intent)).strip()
    task_id_is_full = _is_full_uuid(task_id)
    artifact_list_type = explicit_artifact_list_type(intent)
    if not artifact_list_type and bool(args.get("artifact_lookup")):
        requested_type = str(args.get("artifact_type") or "").strip().casefold()
        artifact_list_type = requested_type if requested_type in {"task", "improvement", "all"} else "all"
    artifact_status = artifact_list_status_filter(intent) if artifact_list_type else ""
    artifact_topic = artifact_topic_query(intent, artifact_list_type) if artifact_list_type else ""
    task = str(args.get("task") or intent or "Retrieve project context.").strip()
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    limit = max(1, min(50, int(args.get("limit") or args.get("max_items_per_layer") or 10)))

    route = {
        "tool": "enrich_task_with_context",
        "intent_type": "enrich_context",
        "mutating": False,
        "confidence": 0.72,
        "reason": "General project-context intent maps to enrich_task_with_context.",
        "matched_example": "",
        "route_candidates": [],
        "scorer": scorer_meta or {"backend_requested": _facade_backend_requested(args), "backend_used": "lexical", "llm_attempted": False, "fallback_reason": ""},
        "payload": {
            "project_id": project,
            "task": task,
            "detail": detail,
            "context_profile": args.get("context_profile") or "hot_path",
            "max_components": max(1, min(20, int(args.get("max_components") or 5))),
        },
    }

    route_args = {**args, "task_id": task_id} if task_id else args
    catalog_route, route_candidates = _selected_catalog_route(intent, PROJECT_CONTEXT_ROUTE_CATALOG, route_args)
    if artifact_list_type:
        catalog_route = _catalog_route_by_intent(PROJECT_CONTEXT_ROUTE_CATALOG, "task_status_list" if artifact_list_type == "task" and artifact_status else "artifact_lookup") or catalog_route
        route_candidates = [
            {
                "intent_type": str(catalog_route.get("intent_type") if catalog_route else "artifact_lookup"),
                "tool": "list_artifacts",
                "score": 1.0,
                "matched_example": "status-filtered artifact list" if artifact_status else "artifact list",
                "scorer": "lexical",
            },
            *[
                item
                for item in route_candidates
                if item.get("intent_type") not in {"artifact_lookup", "task_status_list"}
            ],
        ][:3]
    if llm_decision and _catalog_route_by_intent(PROJECT_CONTEXT_ROUTE_CATALOG, str(llm_decision.get("intent_type") or "")):
        chosen = _catalog_route_by_intent(PROJECT_CONTEXT_ROUTE_CATALOG, str(llm_decision.get("intent_type") or ""))
        assert chosen is not None
        if not artifact_list_type:
            catalog_route = chosen
            route_candidates = [
                {
                    "intent_type": chosen["intent_type"],
                    "tool": chosen["tool"],
                    "score": round(float(llm_decision.get("confidence") or 0.0), 3),
                    "matched_example": str(llm_decision.get("matched_example") or "llm_disambiguation"),
                    "scorer": "llm",
                },
                *[item for item in route_candidates if item.get("intent_type") != chosen["intent_type"]],
            ][:3]
    route["route_candidates"] = route_candidates
    if catalog_route and route_candidates:
        route.update(
            tool=catalog_route["tool"],
            intent_type=catalog_route["intent_type"],
            confidence=round(min(0.95, 0.55 + float(route_candidates[0].get("score") or 0.0) * 0.4), 2),
            mutating=bool(catalog_route.get("mutating")),
            reason=catalog_route["reason"],
            matched_example=route_candidates[0].get("matched_example", ""),
        )
        payload_template = catalog_route.get("payload_template") if isinstance(catalog_route.get("payload_template"), dict) else {}
        if payload_template:
            route["payload"] = _render_route_payload_template(
                payload_template,
                args=args,
                project=project,
                intent=intent,
                limit=limit,
                learned_payload=_learned_payload_from_decision(llm_decision),
            )
        if catalog_route.get("structural_arg") and args.get(str(catalog_route.get("structural_arg"))):
            route["structural_match"] = True

    if task_id and not task_id_is_full:
        route.update(
            tool="list_artifacts",
            intent_type="task_lookup",
            structural_match=True,
            confidence=0.8,
            reason="A partial task-id-like token was provided; list task artifacts so the agent can resolve the exact task_id before replay.",
            warnings=[
                "Partial task_id detected; resolve the exact task_id from result.items before calling pull_task_context."
            ],
            payload={
                "project": project,
                "type": "task",
                "limit": min(max(10, int(args.get("limit") or 50)), 200),
            },
        )
    elif artifact_list_type:
        semantic_lookup = bool(artifact_topic and not artifact_status)
        route.update(
            tool="list_artifacts",
            intent_type=(
                "task_status_list"
                if artifact_list_type == "task" and artifact_status
                else "semantic_artifact_lookup"
                if semantic_lookup
                else "artifact_lookup"
            ),
            structural_match=True,
            confidence=max(0.9, float(route.get("confidence") or 0.0)),
            reason=(
                "Meaning-based artifact lookup uses Qdrant for candidates and SQLite for authoritative rehydration."
                if semantic_lookup
                else "Task or artifact list requests map to unified artifact search; single-task replay requires an explicit task_id."
            ),
            payload={
                "project": project,
                "type": None if artifact_list_type == "all" else artifact_list_type,
                "status": artifact_status,
                "query": artifact_topic,
                **({"search_mode": "semantic"} if semantic_lookup else {}),
                "limit": min(max(1, int(args.get("limit") or 50)), 200),
            },
        )
    elif task_id_is_full and (route["intent_type"] == "task_details" or route["intent_type"] == "enrich_context"):
        route.update(
            tool="pull_task_context",
            intent_type="task_details",
            structural_match=True,
            confidence=max(0.88, float(route.get("confidence") or 0.0)),
            reason="A concrete task-id context request maps to pull_task_context so the agent receives the task replay bundle directly.",
            payload={
                "project": project,
                "task_id": task_id,
                "detail": detail,
                "agent_id": args.get("agent_id") or "codex",
                "include_replay_bundle": bool(args.get("include_replay_bundle", detail == "full")),
            },
        )
    elif route["intent_type"] == "adherence_context":
        route.update(
            tool="enrich_task_with_context",
            intent_type="adherence_context",
            confidence=max(0.9, float(route.get("confidence") or 0.0)),
            reason="Adherence-only natural reads need context and cue guidance, not verification execution.",
            payload={
                "project_id": project,
                "task": task,
                "detail": detail,
                "context_profile": args.get("context_profile") or "hot_path",
                "max_components": max(1, min(20, int(args.get("max_components") or 5))),
            },
        )
    elif route["intent_type"] == "rules_context" or any(term in text for term in ("law", "laws", "rule", "rules", "constraint", "constraints")):
        route.update(
            tool="project_rules",
            intent_type="rules_context",
            confidence=0.85,
            reason="Rule/constraint context belongs to the project_rules facade.",
            payload={
                "project": project,
                "intent": args.get("intent") or "check active project laws",
                "status": args.get("status") or "active",
                "limit": min(max(1, int(args.get("limit") or 100)), 200),
            },
        )
    elif route["intent_type"] == "reconstruction_bundle" or any(term in text for term in ("reconstruct", "reconstruction", "source loss", "lost source")):
        route.update(
            tool="get_project_reconstruction_bundle",
            intent_type="reconstruction_bundle",
            confidence=0.86,
            reason="Source-loss or recovery context maps to the reconstruction bundle.",
            payload={
                "project_id": project,
                "detail": detail,
                "max_items_per_layer": limit,
            },
        )
    elif route["intent_type"] == "project_readiness" or any(term in text for term in ("readiness", "ready", "bootstrap", "onboard")):
        route.update(
            tool="get_project_readiness",
            intent_type="project_readiness",
            confidence=0.82,
            reason="Readiness/bootstrap context maps to get_project_readiness.",
            payload={"project_id": project},
        )

    route["payload"] = {key: value for key, value in route["payload"].items() if value not in (None, "")}
    return route



def project_verify_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = str(args.get("intent") or args.get("task") or "").strip()
    text = intent.casefold()
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    changed_files = _string_list_arg(args.get("changed_files"))
    state = str(args.get("state") or "").strip()
    if state not in {"planning", "implementation", "verification", "live_validation", "documentation", "checkpointing", "handoff", "operator_review"}:
        state = "live_validation" if any(term in text for term in ("restart", "health", "live", "server")) else "verification"
    task = str(args.get("task") or intent or "Verify current project work.").strip()

    route = {
        "tool": "get_task_execution_context",
        "intent_type": "verification_context",
        "mutating": False,
        "confidence": 0.82,
        "reason": "Verification/test intent first needs state-scoped project rules, approved verification contour refs, and risk controls.",
        "matched_example": "",
        "route_candidates": [],
        "scorer": scorer_meta or {"backend_requested": _facade_backend_requested(args), "backend_used": "lexical", "llm_attempted": False, "fallback_reason": ""},
        "payload": {
            "project": project,
            "task_id": str(args.get("task_id") or "").strip(),
            "task": task,
            "state": state,
            "intent": intent,
            "changed_files": changed_files,
            "include_rules": True,
            "include_tools": True,
            "prior_stage_recorded": bool(args.get("prior_stage_recorded")) if "prior_stage_recorded" in args else None,
            "stage_evidence": _string_list_arg(args.get("stage_evidence")),
        },
    }

    catalog_route, route_candidates = _selected_catalog_route(intent, PROJECT_VERIFY_ROUTE_CATALOG, args)
    if llm_decision and _catalog_route_by_intent(PROJECT_VERIFY_ROUTE_CATALOG, str(llm_decision.get("intent_type") or "")):
        chosen = _catalog_route_by_intent(PROJECT_VERIFY_ROUTE_CATALOG, str(llm_decision.get("intent_type") or ""))
        assert chosen is not None
        catalog_route = chosen
        route_candidates = [
            {
                "intent_type": chosen["intent_type"],
                "tool": chosen["tool"],
                "score": round(float(llm_decision.get("confidence") or 0.0), 3),
                "matched_example": str(llm_decision.get("matched_example") or "llm_disambiguation"),
                "scorer": "llm",
            },
            *[item for item in route_candidates if item.get("intent_type") != chosen["intent_type"]],
        ][:3]
    route["route_candidates"] = route_candidates
    if catalog_route and route_candidates:
        route.update(
            tool=catalog_route["tool"],
            intent_type=catalog_route["intent_type"],
            confidence=round(min(0.95, 0.55 + float(route_candidates[0].get("score") or 0.0) * 0.4), 2),
            mutating=bool(catalog_route.get("mutating")),
            reason=catalog_route["reason"],
            matched_example=route_candidates[0].get("matched_example", ""),
        )

    if route["intent_type"] == "health_check" or (
        any(term in text for term in ("health", "healthcheck", "status server", "server status")) and not any(term in text for term in ("restart", "test", "pytest"))
    ):
        route.update(
            tool="memory_health",
            intent_type="health_check",
            confidence=0.8,
            reason="Pure server health intent maps to the read-only health endpoint.",
            payload={},
        )
    elif route["intent_type"] == "restart_validation_plan" or "restart" in text:
        route.update(
            intent_type="restart_validation_plan",
            confidence=0.88,
            reason="Restart/live validation maps to execution context; external restart remains outside MCP and must observe the project-defined restart validation window.",
        )

    route["payload"] = {key: value for key, value in route["payload"].items() if value not in (None, "", [])}
    return route



def project_capture_route(
    args: dict[str, Any],
    *,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = str(args.get("intent") or args.get("summary") or args.get("raw_notes") or "").strip()
    text = intent.casefold()
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args.get("task_id") or "").strip()
    work_id = str(args.get("work_id") or "").strip()
    changed_files = _string_list_arg(args.get("changed_files"))
    verification = _string_list_arg(args.get("verification"))
    raw_notes = str(args.get("raw_notes") or args.get("summary") or intent or "Capture project work.").strip()
    draft_from_spans = (
        not str(args.get("raw_notes") or args.get("summary") or "").strip()
        and any(term in text for term in ("stenographer span", "stenographer spans", "captured spans", "transcript spans"))
        and any(term in text for term in ("draft", "clerk", "checkpoint draft", "from spans"))
    )

    route = {
        "tool": "clerk_draft_report",
        "intent_type": "draft_capture",
        "mutating": False,
        "confidence": 0.78,
        "reason": "Draft/capture intent maps to a reviewable clerk draft before governed memory mutation.",
        "matched_example": "",
        "route_candidates": [],
        "scorer": scorer_meta or {"backend_requested": _facade_backend_requested(args), "backend_used": "lexical", "llm_attempted": False, "fallback_reason": ""},
        "payload": {
            "project": project,
            "task_id": task_id,
            "work_id": work_id,
            "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
            "task_title": str(args.get("task_title") or intent or "Project capture").strip()[:160],
            "raw_notes": raw_notes,
            "stage": str(args.get("stage") or "implementation").strip(),
            "status": str(args.get("status") or "active").strip(),
            "changed_files": changed_files,
            "verification": verification,
            "use_llm": bool(args.get("use_llm", False)),
        },
    }

    catalog_route, route_candidates = _selected_catalog_route(intent, PROJECT_CAPTURE_ROUTE_CATALOG, args)
    if llm_decision and _catalog_route_by_intent(PROJECT_CAPTURE_ROUTE_CATALOG, str(llm_decision.get("intent_type") or "")):
        chosen = _catalog_route_by_intent(PROJECT_CAPTURE_ROUTE_CATALOG, str(llm_decision.get("intent_type") or ""))
        assert chosen is not None
        catalog_route = chosen
        route_candidates = [
            {
                "intent_type": chosen["intent_type"],
                "tool": chosen["tool"],
                "score": round(float(llm_decision.get("confidence") or 0.0), 3),
                "matched_example": str(llm_decision.get("matched_example") or "llm_disambiguation"),
                "scorer": "llm",
            },
            *[item for item in route_candidates if item.get("intent_type") != chosen["intent_type"]],
        ][:3]
    route["route_candidates"] = route_candidates
    if catalog_route and route_candidates:
        route.update(
            tool=catalog_route["tool"],
            intent_type=catalog_route["intent_type"],
            confidence=round(min(0.95, 0.55 + float(route_candidates[0].get("score") or 0.0) * 0.4), 2),
            mutating=bool(catalog_route.get("mutating")),
            reason=catalog_route["reason"],
            matched_example=route_candidates[0].get("matched_example", ""),
        )
    if route["intent_type"] == "record_stenographer_span" and not args.get("span_type"):
        route.update(
            tool="clerk_draft_report",
            intent_type="draft_capture",
            mutating=False,
            confidence=0.72,
            reason="Span recording requires span_type; falling back to a reviewable clerk draft.",
        )

    if route["intent_type"] == "list_stenographer_spans" or (
        not draft_from_spans
        and any(term in text for term in ("list spans", "show spans", "stenographer spans", "transcript spans"))
    ):
        route.update(
            tool="list_stenographer_spans",
            intent_type="list_stenographer_spans",
            confidence=0.84,
            reason="Span inspection intent maps to list_stenographer_spans.",
            payload={
                "project": project,
                "task_id": task_id,
                "work_id": work_id,
                "limit": max(1, min(200, int(args.get("limit") or 50))),
            },
        )
    elif (route["intent_type"] == "record_stenographer_span" and args.get("span_type")) or (any(term in text for term in ("record span", "stenographer", "scribe span", "capture span")) and args.get("span_type")):
        route.update(
            tool="record_stenographer_span",
            intent_type="record_stenographer_span",
            mutating=True,
            confidence=0.86,
            reason="Recording a stenographer span mutates session capture state and is guarded.",
            payload={
                "project": project,
                "task_id": task_id,
                "work_id": work_id,
                "span_type": args.get("span_type"),
                "content": raw_notes,
                "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
                "source": args.get("source") or "project_capture",
            },
        )
    elif route["intent_type"] == "record_work_result" or any(term in text for term in ("save", "record result", "work result", "handoff", "close")) or (
        "checkpoint" in text and not any(term in text for term in ("draft", "preview"))
    ):
        route.update(
            tool="record_work_result",
            intent_type="record_work_result",
            mutating=True,
            confidence=0.88,
            reason="Persisting work result/checkpoint mutates governed project memory and is guarded.",
            payload={
                "project": project,
                "task_id": task_id,
                "artifact_key": str(args.get("artifact_key") or "").strip(),
                "summary": raw_notes,
                "changed_files": changed_files,
                "verification": verification,
                "stage": str(args.get("stage") or "in_progress").strip(),
                "checkpoint_mode": args.get("checkpoint_mode") or "standard",
                "next_step": args.get("next_step") or "",
                "next_step_scope": args.get("next_step_scope") or "unknown",
                "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
                "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
                "work_token": str(args.get("work_token") or "").strip(),
                "source": "project_capture",
                "danger_mode": bool(args.get("danger_mode", False)),
                "danger_confirmation": str(args.get("danger_confirmation") or ""),
            },
        )

    if route["tool"] == "clerk_draft_report" and draft_from_spans:
        route["payload"].pop("raw_notes", None)

    route["payload"] = {
        key: value
        for key, value in route["payload"].items()
        if value not in (None, "", []) or key in ("danger_mode", "danger_confirmation")
    }
    return route



