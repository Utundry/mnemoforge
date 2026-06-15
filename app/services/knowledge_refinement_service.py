from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.governed_refinement_lifecycle import (
    build_refinement_lifecycle,
    complete_refinement_lifecycle,
)

GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PatchCallback = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


LIVE_DB_TARGETS = {"law", "task", "improvement", "artifact", "route_pattern", "memory", "alias", "cue"}
SPEC_TARGETS = {"spec", "spec_instinct", "runtime_spec"}
SUPPORTED_REFINEMENT_TYPES = {
    "metadata_update",
    "applicability_update",
    "status_update",
    "resolve",
    "reopen",
    "supersede",
    "quarantine",
    "spec_change_request",
}
SAFE_LAW_STATUSES = {"observed", "proposed", "reviewed", "user_confirmed", "active", "suppressed", "superseded", "archived"}
RESOLVE_STATUSES = {"done", "resolved", "completed", "complete"}
REOPEN_STATUSES = {"open", "active", "planning", "reopen", "reopened"}
SUPPORTED_LIVE_TYPES = {"law", "task", "improvement", "artifact"}


@dataclass(frozen=True)
class KnowledgeRefinementRequest:
    project: str
    target_ref: str
    target_type: str
    refinement_type: str
    reason: str
    apply: bool
    actor: str
    patch: dict[str, Any]
    status: str
    tags: tuple[str, ...]
    remove_tags: tuple[str, ...]
    topic_path: str
    action_applicability: tuple[str, ...]
    stage_applicability: tuple[str, ...]
    superseded_by: str
    evidence_refs: tuple[str, ...]
    lifecycle: dict[str, Any]


def build_knowledge_refinement_request(
    *,
    project: str,
    payload: dict[str, Any],
    actor: str,
) -> KnowledgeRefinementRequest:
    target_ref = str(payload.get("target_ref") or "").strip()
    target_type = str(payload.get("target_type") or "").strip().lower()
    if not target_type:
        target_type = infer_target_type(target_ref)
    refinement_type = str(payload.get("refinement_type") or "").strip().lower()
    raw_patch = payload.get("patch")
    patch = dict(raw_patch) if isinstance(raw_patch, dict) else {}
    resolved_project = str(payload.get("project") or project or "").strip() or "mnemoforge"
    resolved_target_type = target_type or "unknown"
    return KnowledgeRefinementRequest(
        project=resolved_project,
        target_ref=target_ref,
        target_type=resolved_target_type,
        refinement_type=refinement_type,
        reason=str(payload.get("reason") or "").strip(),
        apply=bool(payload.get("apply", False)),
        actor=actor,
        patch=patch,
        status=str(payload.get("status") or patch.get("status") or "").strip().lower(),
        tags=tuple(_string_list(payload.get("tags") if "tags" in payload else patch.get("tags"))),
        remove_tags=tuple(_string_list(payload.get("remove_tags") if "remove_tags" in payload else patch.get("remove_tags"))),
        topic_path=str(payload.get("topic_path") or patch.get("topic_path") or "").strip(),
        action_applicability=tuple(
            _string_list(payload.get("action_applicability") if "action_applicability" in payload else patch.get("action_applicability"))
        ),
        stage_applicability=tuple(
            _string_list(payload.get("stage_applicability") if "stage_applicability" in payload else patch.get("stage_applicability"))
        ),
        superseded_by=str(payload.get("superseded_by") or patch.get("superseded_by") or "").strip(),
        evidence_refs=tuple(_string_list(payload.get("evidence_refs") if "evidence_refs" in payload else patch.get("evidence_refs"))),
        lifecycle=build_refinement_lifecycle(
            project=resolved_project,
            payload=payload,
            target_ref=target_ref,
            target_type=resolved_target_type,
            action=refinement_type,
            actor=actor,
            adapter="knowledge_refinement",
            default_expected="The governed target should match the approved refinement without bypassing its target-specific adapter.",
        ),
    )


def infer_target_type(target_ref: str) -> str:
    prefix = str(target_ref or "").split(":", 1)[0].strip().lower()
    if prefix in {"law", "task", "improvement", "artifact", "route_pattern", "memory", "alias", "cue", "spec"}:
        return prefix
    if str(target_ref or "").strip().endswith(".json"):
        return "spec"
    return "unknown"


async def build_knowledge_refinement_packet(
    *,
    request: KnowledgeRefinementRequest,
    api_base: str,
    get: GetCallback | None,
    patch: PatchCallback | None,
    post: PostCallback,
) -> dict[str, Any]:
    validation = validate_refinement_request(request)
    if validation:
        return _attach_lifecycle(request, validation)
    if request.target_type in SPEC_TARGETS:
        result = _spec_change_packet(request)
    elif not request.apply:
        result = _preview_packet(request)
    elif request.target_type == "law":
        result = await _apply_law_refinement(request, api_base=api_base, get=get, patch=patch, post=post)
    elif request.target_type in {"task", "improvement", "artifact"}:
        result = await _apply_unified_artifact_refinement(request, api_base=api_base, post=post)
    else:
        result = _unsupported_live_target_packet(request)
    return _attach_lifecycle(request, result)


def validate_refinement_request(request: KnowledgeRefinementRequest) -> dict[str, Any] | None:
    missing = []
    if not request.project:
        missing.append("project")
    if not request.target_ref:
        missing.append("target_ref")
    if not request.reason:
        missing.append("reason")
    if request.refinement_type not in SUPPORTED_REFINEMENT_TYPES:
        missing.append("refinement_type")
    if missing:
        return {
            "status": "needs_input",
            "message": "Knowledge refinement requires a target, a supported refinement_type, and a reason.",
            "missing_fields": missing,
            "next_safe_action": "Submit knowledge_refinement_feedback with target_ref, refinement_type, and reason.",
        }
    return None


def _preview_packet(request: KnowledgeRefinementRequest) -> dict[str, Any]:
    supported = _apply_support(request)
    if not supported["safe_to_apply"]:
        return {
            "status": "preview_unsupported",
            "target_ref": request.target_ref,
            "target_type": request.target_type,
            "refinement_type": request.refinement_type,
            "apply_required": False,
            "safe_to_apply": False,
            "planned_action": supported["planned_action"],
            "unsupported_reason": supported["reason"],
            "next_safe_action": supported["next_safe_action"],
        }
    return {
        "status": "preview",
        "target_ref": request.target_ref,
        "target_type": request.target_type,
        "refinement_type": request.refinement_type,
        "apply_required": True,
        "safe_to_apply": True,
        "planned_action": supported["planned_action"],
        "next_safe_action": "Review the planned action, then resubmit with apply=true if it matches operator intent.",
    }


def _spec_change_packet(request: KnowledgeRefinementRequest) -> dict[str, Any]:
    change_request = {
        "target_ref": request.target_ref,
        "target_type": request.target_type,
        "requested_change": request.reason,
        "evidence_refs": list(request.evidence_refs),
        "authority": "maintainer_change_request",
    }
    return {
        "status": "blocked_static_spec",
        "target_ref": request.target_ref,
        "target_type": request.target_type,
        "refinement_type": request.refinement_type,
        "mutation_executed": False,
        "change_request": change_request,
        "planned_action": "Build a developer feedback packet instead of mutating published runtime specs.",
        "recommended_next_call": {
            "tool": "submit",
            "form_id": "developer_feedback_packet",
            "payload": {
                "project": request.project,
                "title": f"Static spec refinement feedback for {request.target_ref}",
                "area": "mcp_surface",
                "severity": "medium",
                "observed_behavior": request.reason,
                "expected_behavior": "Published runtime specs should remain static during normal use; useful spec feedback should be packaged for maintainers.",
                "impact": "The user may need a maintainer-facing report instead of an internal project backlog mutation.",
                "evidence_refs": list(request.evidence_refs),
                "next_action": "Review the packet, then send or share it with the system maintainers through the project's support channel.",
            },
        },
        "next_safe_action": "Review the developer feedback packet and share it with maintainers; do not edit published specs through runtime learning.",
    }


async def _apply_law_refinement(
    request: KnowledgeRefinementRequest,
    *,
    api_base: str,
    get: GetCallback | None,
    patch: PatchCallback | None,
    post: PostCallback,
) -> dict[str, Any]:
    law_id = _local_id_from_ref(request.target_ref)
    if not law_id:
        return _needs_input_packet(request, "law target_ref must include a law id, for example law:project:id.")
    if request.refinement_type in {"status_update", "supersede", "quarantine"} or request.status:
        status = request.status
        if request.refinement_type == "quarantine" and not status:
            status = "suppressed"
        if request.refinement_type == "supersede" and not status:
            status = "superseded"
        if status not in SAFE_LAW_STATUSES:
            return _needs_input_packet(request, "Unsupported law status for safe refinement.")
        if patch is None:
            return _needs_input_packet(request, "Law status refinement requires server patch support.")
        result = await patch(
            api_base,
            f"/laws/{quote(law_id, safe='')}/status",
            {
                "status": status,
                "reason": request.reason,
                "acted_by": request.actor,
                "action_source": "mailbox_submit.knowledge_refinement_feedback",
            },
        )
        return _applied_packet(request, result=result, action=f"law_status:{status}")

    if patch is None:
        return _needs_input_packet(request, "Law metadata refinement requires server patch support.")
    current = await get(api_base, f"/laws/{quote(law_id, safe='')}") if get is not None else {}
    current_tags = _string_list(current.get("tags") if isinstance(current, dict) else [])
    next_tags = _merge_tags(
        current_tags,
        add=[
            *request.tags,
            *(f"action:{item}" for item in request.action_applicability),
            *(f"stage:{item}" for item in request.stage_applicability),
            *(f"evidence:{item}" for item in request.evidence_refs),
        ],
        remove=request.remove_tags,
    )
    patch_payload: dict[str, Any] = {}
    if next_tags != current_tags or request.tags or request.remove_tags or request.action_applicability or request.stage_applicability:
        patch_payload["tags"] = next_tags
    if request.topic_path:
        patch_payload["topic_path"] = request.topic_path
    if request.superseded_by:
        patch_payload["supersedes"] = _merge_tags(_string_list(current.get("supersedes") if isinstance(current, dict) else []), add=[request.superseded_by])
    if not patch_payload:
        return _needs_input_packet(request, "No safe law metadata fields were provided.")
    result = await patch(api_base, f"/laws/{quote(law_id, safe='')}", patch_payload)
    return _applied_packet(request, result=result, action="law_metadata_update")


async def _apply_unified_artifact_refinement(
    request: KnowledgeRefinementRequest,
    *,
    api_base: str,
    post: PostCallback,
) -> dict[str, Any]:
    artifact_ref = _canonical_artifact_ref(request)
    if not artifact_ref:
        return _needs_input_packet(request, "Artifact target_ref must include a governed artifact key.")
    action = request.refinement_type
    status = request.status
    if action in {"resolve", "status_update"} and (not status or status in RESOLVE_STATUSES):
        result = await post(
            api_base,
            f"/artifacts/{quote(artifact_ref, safe='')}/resolve",
            {
                "acted_by": request.actor,
                "action_source": "mailbox_submit.knowledge_refinement_feedback",
                "reason": request.reason,
            },
        )
        return _applied_packet(request, result=result, action="artifact_resolve")
    if action == "reopen" or status in REOPEN_STATUSES:
        result = await post(
            api_base,
            f"/artifacts/{quote(artifact_ref, safe='')}/reopen",
            {
                "project": request.project,
                "status": "open" if request.target_type == "improvement" else "active",
                "acted_by": request.actor,
                "action_source": "mailbox_submit.knowledge_refinement_feedback",
                "reason": request.reason,
            },
        )
        return _applied_packet(request, result=result, action="artifact_reopen")
    return _needs_input_packet(
        request,
        "Artifact refinement currently supports resolve/done or reopen/open lifecycle updates.",
    )


def _unsupported_live_target_packet(request: KnowledgeRefinementRequest) -> dict[str, Any]:
    return {
        "status": "unsupported_target",
        "target_ref": request.target_ref,
        "target_type": request.target_type,
        "refinement_type": request.refinement_type,
        "mutation_executed": False,
        "next_safe_action": "Use the specialized learning surface for this target type, or create an improvement to add support to the universal refinement contour.",
        "recommended_next_call": _recommended_specialized_call(request),
    }


def _apply_support(request: KnowledgeRefinementRequest) -> dict[str, Any]:
    if request.target_type == "law":
        return {
            "safe_to_apply": True,
            "planned_action": "update_law_status"
            if request.refinement_type in {"status_update", "supersede", "quarantine"} or request.status
            else "update_law_metadata",
            "reason": "",
            "next_safe_action": "Review the planned law refinement, then resubmit with apply=true if it matches operator intent.",
        }
    if request.target_type in {"task", "improvement", "artifact"}:
        status = request.status
        if request.refinement_type in {"resolve", "reopen"}:
            action = "artifact_resolve" if request.refinement_type == "resolve" else "artifact_reopen"
            return {
                "safe_to_apply": True,
                "planned_action": action,
                "reason": "",
                "next_safe_action": "Review the lifecycle refinement, then resubmit with apply=true if it matches operator intent.",
            }
        if request.refinement_type == "status_update" and (status in RESOLVE_STATUSES or status in REOPEN_STATUSES):
            action = "artifact_resolve" if status in RESOLVE_STATUSES else "artifact_reopen"
            return {
                "safe_to_apply": True,
                "planned_action": action,
                "reason": "",
                "next_safe_action": "Review the lifecycle refinement, then resubmit with apply=true if it matches operator intent.",
            }
        return {
            "safe_to_apply": False,
            "planned_action": "unsupported_task_or_improvement_refinement",
            "reason": "Artifact refinement currently supports only resolve/done or reopen/open lifecycle updates.",
            "next_safe_action": "Use task context or record_progress to capture framing gaps; do not resubmit apply=true for this refinement type.",
        }
    return {
        "safe_to_apply": False,
        "planned_action": "route_to_specialized_learning_surface",
        "reason": "This target type is not yet supported by the universal refinement apply path.",
        "next_safe_action": "Use the specialized feedback surface or create maintainer-facing feedback for this target type.",
    }


def _applied_packet(request: KnowledgeRefinementRequest, *, result: dict[str, Any], action: str) -> dict[str, Any]:
    return {
        "status": "applied",
        "target_ref": request.target_ref,
        "target_type": request.target_type,
        "refinement_type": request.refinement_type,
        "applied_action": action,
        "mutation_executed": True,
        "result": result,
        "next_safe_action": "Knowledge refinement was applied. Use get/ref or diagnostic inspection if you need to verify details.",
    }


def _needs_input_packet(request: KnowledgeRefinementRequest, message: str) -> dict[str, Any]:
    return {
        "status": "needs_input",
        "target_ref": request.target_ref,
        "target_type": request.target_type,
        "refinement_type": request.refinement_type,
        "message": message,
        "next_safe_action": "Review the target type and safe fields, then resubmit knowledge_refinement_feedback.",
    }


def _planned_action(request: KnowledgeRefinementRequest) -> str:
    if request.target_type in SPEC_TARGETS:
        return "create_development_work_for_static_spec_change"
    if request.target_type == "law":
        if request.refinement_type in {"status_update", "supersede", "quarantine"} or request.status:
            return "update_law_status"
        return "update_law_metadata"
    if request.target_type in {"task", "improvement", "artifact"}:
        return "update_unified_artifact_lifecycle"
    return "route_to_specialized_learning_surface"


def _recommended_specialized_call(request: KnowledgeRefinementRequest) -> dict[str, Any]:
    if request.target_type == "route_pattern":
        return {
            "tool": "submit",
            "form_id": "route_feedback",
            "payload": {
                "project": request.project,
                "reason": request.reason,
                "pattern_id": _local_id_from_ref(request.target_ref),
            },
        }
    if request.target_type in {"memory", "alias", "cue"}:
        return {
            "tool": "submit",
            "form_id": "store_memory",
            "payload": {
                "project": request.project,
                "content": request.reason,
                "memory_type": "fact",
                "tags": ["knowledge_refinement_feedback", request.target_type],
            },
        }
    return {
        "tool": "submit",
        "form_id": "create_improvement",
        "payload": {
            "project": request.project,
            "title": f"Add refinement support for {request.target_type}",
            "summary": request.reason,
            "next_step": "Extend knowledge_refinement_feedback for this target type using existing governed services.",
        },
    }


def _canonical_artifact_ref(request: KnowledgeRefinementRequest) -> str:
    raw = str(request.target_ref or "").strip()
    if raw.startswith(("task:", "improvement:", "artifact:")):
        return raw
    local_id = _local_id_from_ref(raw)
    if local_id and request.target_type in {"task", "improvement"}:
        return f"{request.target_type}:{request.project}:{local_id}"
    return ""


def _attach_lifecycle(request: KnowledgeRefinementRequest, result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status") or "unknown")
    mutation_executed = bool(result.get("mutation_executed", False))
    applied_result = result.get("result") if isinstance(result.get("result"), dict) else {}
    if status == "applied":
        expected = {"mutation_executed": True, "applied_action": result.get("applied_action")}
        actual = {
            "mutation_executed": mutation_executed,
            "applied_action": result.get("applied_action"),
            "target_status": applied_result.get("status"),
        }
        satisfied: bool | None = mutation_executed and bool(result.get("applied_action"))
    elif status == "blocked_static_spec":
        expected = {"runtime_mutation": False, "change_request": True}
        actual = {
            "runtime_mutation": mutation_executed,
            "change_request": isinstance(result.get("change_request"), dict),
        }
        satisfied = not mutation_executed and actual["change_request"]
    elif status in {"preview", "preview_unsupported", "unsupported_target"}:
        expected = {"runtime_mutation": False}
        actual = {"runtime_mutation": mutation_executed}
        satisfied = not mutation_executed
    else:
        expected = {}
        actual = {}
        satisfied = None
    reversible, reversal_action = _reversal(request, result)
    lifecycle = complete_refinement_lifecycle(
        request.lifecycle,
        status=status,
        mutation_executed=mutation_executed,
        postcondition_expected=expected,
        postcondition_actual=actual,
        postcondition_satisfied=satisfied,
        audit_evidence=[request.target_ref, *request.evidence_refs],
        reversible=reversible,
        reversal_action=reversal_action,
        adapter_kind="static_change_request" if request.target_type in SPEC_TARGETS else "governed_runtime_adapter",
    )
    if request.target_type in SPEC_TARGETS:
        lifecycle["authority"]["mode"] = "maintainer_change_request"
        lifecycle["authority"]["apply_requested"] = False
    result["lifecycle"] = lifecycle
    return result


def _reversal(request: KnowledgeRefinementRequest, result: dict[str, Any]) -> tuple[bool, str]:
    if not result.get("mutation_executed"):
        return False, ""
    action = str(result.get("applied_action") or "")
    if action == "artifact_resolve":
        return True, "reopen"
    if action == "artifact_reopen":
        return True, "resolve"
    if action.startswith("law_status:"):
        return True, "status_update"
    if action == "law_metadata_update":
        return True, "metadata_update_with_previous_values"
    return False, ""


def _local_id_from_ref(target_ref: str) -> str:
    text = str(target_ref or "").strip()
    if not text:
        return ""
    if ":" in text:
        return text.rsplit(":", 1)[-1].strip()
    return text


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _merge_tags(current: list[str], *, add: Any = None, remove: Any = None) -> list[str]:
    removed = {str(item).strip() for item in _string_list(remove) if str(item).strip()}
    result: list[str] = []
    seen: set[str] = set()
    for value in [*current, *_string_list(add)]:
        text = str(value or "").strip()
        if not text or text in removed or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
