from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote


GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PatchCallback = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


LIVE_DB_TARGETS = {"law", "task", "improvement", "route_pattern", "memory", "alias", "cue"}
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
SUPPORTED_LIVE_TYPES = {"law", "task", "improvement"}


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
    return KnowledgeRefinementRequest(
        project=str(payload.get("project") or project or "").strip() or "mnemoforge",
        target_ref=target_ref,
        target_type=target_type or "unknown",
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
    )


def infer_target_type(target_ref: str) -> str:
    prefix = str(target_ref or "").split(":", 1)[0].strip().lower()
    if prefix in {"law", "task", "improvement", "route_pattern", "memory", "alias", "cue", "spec"}:
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
        return validation
    if request.target_type in SPEC_TARGETS:
        return _spec_change_packet(request)
    if not request.apply:
        return _preview_packet(request)
    if request.target_type == "law":
        return await _apply_law_refinement(request, api_base=api_base, get=get, patch=patch, post=post)
    if request.target_type in {"task", "improvement"}:
        return await _apply_unified_artifact_refinement(request, api_base=api_base, post=post)
    return _unsupported_live_target_packet(request)


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
    return {
        "status": "blocked_static_spec",
        "target_ref": request.target_ref,
        "target_type": request.target_type,
        "refinement_type": request.refinement_type,
        "mutation_executed": False,
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
        return _needs_input_packet(request, "Task/improvement target_ref must use task:project:id or improvement:project:id.")
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
                "status": "active" if request.target_type == "task" else "open",
                "acted_by": request.actor,
                "action_source": "mailbox_submit.knowledge_refinement_feedback",
                "reason": request.reason,
            },
        )
        return _applied_packet(request, result=result, action="artifact_reopen")
    return _needs_input_packet(
        request,
        "Task/improvement refinement currently supports resolve/done or reopen/open lifecycle updates.",
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
    if request.target_type in {"task", "improvement"}:
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
            "reason": "Task/improvement refinement currently supports only resolve/done or reopen/open lifecycle updates.",
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
    if request.target_type in {"task", "improvement"}:
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
    if raw.startswith(("task:", "improvement:")):
        return raw
    local_id = _local_id_from_ref(raw)
    if local_id and request.target_type in {"task", "improvement"}:
        return f"{request.target_type}:{request.project}:{local_id}"
    return ""


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
