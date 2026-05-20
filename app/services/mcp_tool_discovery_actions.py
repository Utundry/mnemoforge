from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.mcp_tool_contracts import (
    format_list_tool_families_response,
    format_tool_explain_response,
    format_tool_family_tools_response,
    format_tool_feedback_response,
    format_tool_recommend_response,
)


PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
PayloadBuilder = Callable[..., dict[str, Any]]
ToolStageCallback = Callable[[str], str]
RecordToolFeedbackCallback = Callable[..., Any]
AnnotatePayloadCallback = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolDiscoveryActionDependencies:
    post: PostCallback
    build_tool_families_payload: PayloadBuilder
    build_family_tools_payload: PayloadBuilder
    build_tool_explanation: PayloadBuilder
    build_tool_recommendation: PayloadBuilder
    get_tool_stage: ToolStageCallback
    record_tool_feedback: RecordToolFeedbackCallback
    annotate_payload: AnnotatePayloadCallback


async def execute_tool_discovery_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: ToolDiscoveryActionDependencies,
    session_id: str | None = None,
) -> str:
    if name == "list_tool_families":
        data = dependencies.build_tool_families_payload(
            include_compatibility_note=bool(args.get("include_compatibility_note", True)),
        )
        data = dependencies.annotate_payload(name, data)
        return format_list_tool_families_response(data)

    if name == "tool_family_tools":
        family = str(args["family"]).strip()
        depth = str(args.get("depth", "brief")).strip() or "brief"
        data = dependencies.build_family_tools_payload(
            family,
            depth=depth,
            limit=int(args.get("limit", 12)),
        )
        data = dependencies.annotate_payload(name, data)
        return format_tool_family_tools_response(data)

    if name == "tool_explain":
        tool_name = str(args["tool_name"]).strip()
        task_context = str(args.get("task_context") or "").strip()
        data = dependencies.build_tool_explanation(tool_name, task_context=task_context)
        data = dependencies.annotate_payload(name, data)
        return format_tool_explain_response(data)

    if name == "tool_recommend":
        task = str(args["task"]).strip()
        project_id = str(args.get("project_id") or "").strip()
        top_n = int(args.get("top_n", 3))
        data = dependencies.build_tool_recommendation(task, project_id=project_id, top_n=top_n)
        if project_id:
            try:
                project_bundle = await dependencies.post(
                    api_base,
                    "/project/enrich-task",
                    {
                        "project_id": project_id,
                        "task": task,
                        "max_components": 3,
                    },
                )
                project_calls = project_bundle.get("recommended_mcp_calls") or []
                if project_calls:
                    data["project_recommended_calls"] = project_calls[:top_n]
                    data["project_context_summary"] = str(project_bundle.get("context") or "").strip()[:1200]
            except Exception:
                pass
        data = dependencies.annotate_payload(name, data)
        return format_tool_recommend_response(data)

    if name == "tool_feedback":
        return await _execute_tool_feedback(
            args=args,
            dependencies=dependencies,
            session_id=session_id,
        )

    raise ValueError(f"Unsupported tool discovery action: {name}")


async def _execute_tool_feedback(
    *,
    args: dict[str, Any],
    dependencies: ToolDiscoveryActionDependencies,
    session_id: str | None,
) -> str:
    from app.services.learning_store import get_learning_store

    tool_name = str(args["tool_name"]).strip()
    tool_stage = str(args.get("tool_stage") or dependencies.get_tool_stage(tool_name)).strip() or "testing"
    valence = str(args["valence"]).strip().lower()
    worked = bool(args.get("worked", valence == "positive"))
    scope = str(args.get("scope") or "").strip()
    what_was_tested = str(args.get("what_was_tested") or "").strip()
    expected_behavior = str(args.get("expected_behavior") or "").strip()
    observed_behavior = str(args.get("observed_behavior") or "").strip()
    friction = str(args.get("friction") or "").strip()
    suggestion = str(args.get("suggestion") or "").strip()
    next_action = str(args.get("next_action") or "").strip()
    assessment = str(args.get("assessment") or "").strip()
    task_context = str(args.get("task_context") or "").strip()
    missing_fields = args.get("missing_fields") or []
    if isinstance(missing_fields, str):
        missing_fields = [missing_fields]
    payload = {
        "tool_name": tool_name,
        "tool_stage": tool_stage,
        "project_id": str(args.get("project_id") or "").strip(),
        "task_context": task_context,
        "friction": friction,
        "suggestion": suggestion,
        "missing_fields": [str(item).strip() for item in missing_fields if str(item).strip()],
        "worked": worked,
        "agent_id": str(args.get("agent_id") or "mcp-agent").strip() or "mcp-agent",
        "session_id": str(args.get("session_id") or session_id or "").strip(),
    }
    valence_for_store = "positive" if worked and valence != "negative" else "negative"
    magnitude = 0.9 if valence_for_store == "positive" else 0.4
    store = get_learning_store()
    feedback_id = await store.write_feedback(
        valence=valence_for_store,
        episode_id=payload["session_id"],
        magnitude=magnitude,
        source="mcp_tool_feedback",
        payload=payload,
    )
    try:
        dependencies.record_tool_feedback(
            tool_name=tool_name,
            valence=valence_for_store,
            tool_stage=tool_stage,
            worked=worked,
            friction=friction,
            suggestion=suggestion,
            task_context=task_context,
            project_id=payload["project_id"],
            agent_id=payload["agent_id"],
            session_id=payload["session_id"],
            missing_fields=payload["missing_fields"],
        )
    except Exception:
        pass
    try:
        await store.write_event(
            event_type="artifact_feedback",
            agent_id=payload["agent_id"],
            project=payload["project_id"],
            transport="mcp",
            episode_id=payload["session_id"],
            context_signature=f"tool={tool_name};stage={tool_stage};transport=mcp",
            payload={
                "tool_name": tool_name,
                "tool_stage": tool_stage,
                "valence": valence_for_store,
                "worked": worked,
                "friction": friction,
                "suggestion": suggestion,
                "missing_fields": payload["missing_fields"],
                "task_context": task_context,
            },
        )
    except Exception:
        pass
    data = build_tool_feedback_envelope(
        tool_name=tool_name,
        tool_stage=tool_stage,
        valence=valence_for_store,
        worked=worked,
        friction=friction,
        suggestion=suggestion,
        task_context=task_context,
        project_id=payload["project_id"],
        agent_id=payload["agent_id"],
        session_id=payload["session_id"],
        missing_fields=payload["missing_fields"],
        feedback_id=feedback_id,
        assessment=assessment or None,
        scope=scope,
        what_was_tested=what_was_tested,
        expected_behavior=expected_behavior,
        observed_behavior=observed_behavior,
        next_action=next_action,
    )
    data = dependencies.annotate_payload("tool_feedback", data)
    return format_tool_feedback_response(data)


def build_tool_feedback_envelope(
    *,
    tool_name: str,
    tool_stage: str,
    valence: str,
    worked: bool,
    friction: str,
    suggestion: str,
    task_context: str,
    project_id: str,
    agent_id: str,
    session_id: str,
    missing_fields: list[str],
    feedback_id: int | str,
    assessment: str | None = None,
    scope: str = "",
    what_was_tested: str = "",
    expected_behavior: str = "",
    observed_behavior: str = "",
    next_action: str = "",
    should_promote: bool | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    normalized_stage = str(tool_stage or "testing").strip().lower() or "testing"
    normalized_valence = str(valence or "mixed").strip().lower() or "mixed"
    normalized_friction = str(friction or "").strip()
    normalized_suggestion = str(suggestion or "").strip()
    normalized_task_context = str(task_context or "").strip()
    normalized_scope = str(scope or "").strip()
    normalized_what_was_tested = str(what_was_tested or "").strip()
    normalized_expected_behavior = str(expected_behavior or "").strip()
    normalized_observed_behavior = str(observed_behavior or "").strip()
    normalized_next_action = str(next_action or "").strip()
    normalized_missing_fields = [str(item).strip() for item in missing_fields if str(item).strip()]

    if should_promote is None:
        should_promote = worked and not normalized_friction and not normalized_missing_fields and normalized_stage == "testing"
    if assessment:
        normalized_assessment = assessment
    elif normalized_stage != "testing":
        normalized_assessment = "informational"
    elif not worked:
        normalized_assessment = "needs_redesign" if (normalized_friction or normalized_missing_fields) else "keep_testing"
    elif normalized_friction or normalized_missing_fields:
        normalized_assessment = "keep_testing"
    elif should_promote:
        normalized_assessment = "promote_candidate"
    else:
        normalized_assessment = "keep_testing"

    if not normalized_scope:
        normalized_scope = f"testing {tool_name}"
    if not normalized_what_was_tested:
        normalized_what_was_tested = normalized_task_context or f"Use of {tool_name}"
    if not normalized_expected_behavior:
        normalized_expected_behavior = "Tool should complete the requested path and expose the needed affordances clearly."
    if not normalized_observed_behavior:
        normalized_observed_behavior = "Tool completed the path." if worked else "Tool did not complete the requested path."
    if not normalized_next_action:
        normalized_next_action = {
            "promote_candidate": "Broaden usage, keep monitoring, and consider promotion if signal stays clean.",
            "keep_testing": "Tighten affordances or wording, then retest the same path.",
            "needs_redesign": "Redesign the interface or missing fields before retesting.",
            "deprecate": "Deprecate after confirming there is no better canonical path.",
            "informational": "Use this as an observational note; no promotion decision implied.",
        }.get(normalized_assessment, "Retest with clearer expectations.")
    if confidence is None:
        confidence = 0.9 if normalized_assessment == "promote_candidate" else 0.75 if normalized_assessment == "keep_testing" else 0.45

    return {
        "summary": f"Recorded tool feedback for {tool_name}",
        "feedback_id": feedback_id,
        "tool_name": tool_name,
        "tool_stage": normalized_stage,
        "valence": normalized_valence,
        "worked": worked,
        "assessment": normalized_assessment,
        "should_promote": bool(should_promote),
        "confidence": round(float(confidence), 2),
        "scope": normalized_scope,
        "what_was_tested": normalized_what_was_tested,
        "expected_behavior": normalized_expected_behavior,
        "observed_behavior": normalized_observed_behavior,
        "friction": normalized_friction,
        "suggestion": normalized_suggestion,
        "next_action": normalized_next_action,
        "missing_fields": normalized_missing_fields,
        "task_context": normalized_task_context,
        "project_id": str(project_id or "").strip(),
        "agent_id": str(agent_id or "").strip(),
        "session_id": str(session_id or "").strip(),
    }
