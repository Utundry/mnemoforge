from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.context_page_store import compact_page, get_context_page_store
from app.services.mcp_response_filter import filter_mcp_response
from app.services.project_identity_service import project_identity_envelope, resolve_project_id
from app.services.replay_completeness_service import (
    build_replay_drill_decision,
    build_token_budget,
    evaluate_execution_readiness,
    evaluate_replay_completeness,
)
from app.services.stenography_protocol_service import build_stenography_coverage, build_stenography_protocol
from app.services.unified_artifact_service import task_has_closeout_evidence


GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PullTaskContextDependencies:
    get: GetCallback
    post: PostCallback


def _parse_task_checkpoint_change(change: dict[str, Any] | None) -> dict[str, Any] | None:
    if not change:
        return None
    content = str(change.get("content") or "")
    parsed: dict[str, Any] = {
        "id": change.get("id"),
        "timestamp": change.get("timestamp"),
        "tags": change.get("tags") or [],
        "raw_content": content,
    }
    list_fields = {
        "Blockers": "blockers",
        "Decisions": "decisions",
        "Changed files": "changed_files",
        "Verification": "verification",
        "Remaining risk": "remaining_risk",
    }
    scalar_fields = {
        "Checkpoint stage": "stage",
        "Checkpoint status": "status",
        "Summary": "summary",
        "Next step": "next_step",
        "Next step scope": "next_step_scope",
        "Reason": "reason",
    }
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in scalar_fields:
            parsed[scalar_fields[key]] = value
        elif key in list_fields:
            parsed[list_fields[key]] = [item.strip() for item in value.split(";") if item.strip()]
    return parsed


def _compact_task_history(changes: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for change in (changes or [])[-limit:]:
        compact.append(
            {
                "id": change.get("id"),
                "change_type": change.get("change_type"),
                "timestamp": change.get("timestamp"),
                "content": str(change.get("content") or "").strip(),
                "why": str(change.get("why") or "").strip(),
                "agent_id": change.get("agent_id"),
                "source": change.get("source"),
                "tags": change.get("tags") or [],
            }
        )
    return compact


async def _fetch_linked_improvement_bundle(api_base: str, project: str, linked_improvement_id: str, *, dependencies: PullTaskContextDependencies) -> dict[str, Any] | None:
    linked_id = str(linked_improvement_id or "").strip()
    if not linked_id:
        return None
    artifact_key = f"improvement:{project}:{linked_id}"
    try:
        artifact = await dependencies.get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
    except Exception:
        return {
            "artifact_key": artifact_key,
            "id": linked_id,
            "available": False,
        }
    return {
        "artifact_key": artifact_key,
        "id": linked_id,
        "available": True,
        "title": artifact.get("title"),
        "status": artifact.get("status"),
        "stage": artifact.get("stage"),
        "verdict": artifact.get("verdict"),
        "linked_artifact_key": artifact.get("linked_artifact_key"),
        "linked_status": artifact.get("linked_status"),
    }


def _task_context_page_parent_refs(*, project: str, task_id: str) -> list[str]:
    refs: list[str] = []
    for candidate_project in (project, resolve_project_id(project)):
        normalized_project = str(candidate_project or "").strip()
        if not normalized_project:
            continue
        parent_ref = f"task:{normalized_project}:{task_id}"
        if parent_ref not in refs:
            refs.append(parent_ref)
    return refs


def _task_context_pages(*, project: str, task_id: str, include_content: bool = True, limit: int = 20) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    seen_page_ids: set[str] = set()
    parent_refs = _task_context_page_parent_refs(project=project, task_id=task_id)
    store = get_context_page_store()
    for parent_ref in parent_refs:
        for page in store.list_pages(parent_ref=parent_ref, include_history=False, limit=limit):
            page_id = str(page.get("page_id") or "").strip()
            if not page_id or page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            pages.append(page)
    if not pages:
        return {}
    return {
        "source_of_truth": "sqlite",
        "index_role": "qdrant_derived_active_pages_only",
        "parent_refs": parent_refs,
        "count": len(pages),
        "pages": [compact_page(page, include_content=include_content) for page in pages],
    }


def _compact_task_context_pages(context_pages: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context_pages, dict) or not context_pages.get("pages"):
        return {}
    compact = dict(context_pages)
    compact["pages"] = [
        compact_page(page, include_content=False) if isinstance(page, dict) else page
        for page in (context_pages.get("pages") or [])
    ]
    return compact


def _checkpoint_stage_for_state(state: str) -> str:
    normalized = str(state or "").strip().lower()
    if normalized in {"planning", "checkpointing"}:
        return "planning"
    if normalized in {"implementation", "verification", "live_validation", "documentation", "operator_review"}:
        return "in_progress"
    if normalized == "handoff":
        return "handoff"
    return "in_progress"


def _operational_tray_target_tool(tray_action: str) -> str:
    return {
        "record_stage_evidence": "record_task_checkpoint",
        "record_checkpoint": "record_task_checkpoint",
        "draft_checkpoint": "clerk_draft_report",
        "review_rule_candidates": "get_rule_candidate_review_packet",
        "list_rule_candidates": "list_rule_candidates",
    }.get(str(tray_action or "").strip(), "")


def _build_replay_bundle(
    *,
    project: str,
    task_id: str,
    statement: dict[str, Any],
    changes: list[dict[str, Any]],
    handoffs: list[dict[str, Any]],
    linked_improvement: dict[str, Any] | None,
    context_pages: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = statement.get("quality") or {}
    context_page_refs = [
        str(page.get("page_ref") or "").strip()
        for page in ((context_pages or {}).get("pages") or [])
        if isinstance(page, dict) and str(page.get("page_ref") or "").strip()
    ]
    project_context_refs = {
        "project_id": project,
        "task_id": task_id,
        "grounded_by": quality.get("grounded_by") or [],
        "readiness_tool": "get_project_readiness",
        "enrichment_tool": "enrich_task_with_context",
    }
    if context_page_refs:
        project_context_refs["context_pages"] = context_page_refs
    return {
        "task_history": _compact_task_history(changes),
        "linked_improvement": linked_improvement,
        "handoff_refs": [
            {
                "memory_id": item.get("memory_id"),
                "handoff_label": item.get("handoff_label"),
                "status": item.get("status"),
                "phase": item.get("phase"),
                "task_id": item.get("task_id"),
            }
            for item in handoffs[:5]
        ],
        "project_context_refs": project_context_refs,
    }


def _estimate_response_tokens(payload: dict[str, Any], budget_args: dict[str, Any] | None = None) -> dict[str, Any]:
    response_chars = len(json.dumps(payload, ensure_ascii=False))
    return build_token_budget(response_chars=response_chars, **(budget_args or {}))


def _layer_summary(full_payload: dict[str, Any]) -> dict[str, Any]:
    bundle = full_payload.get("replay_bundle") or {}
    return {
        "task_history": {
            "available": bool(bundle.get("task_history")),
            "count": len(bundle.get("task_history") or []),
            "request": {"detail": "full"},
        },
        "linked_improvement": {
            "available": bool(bundle.get("linked_improvement")),
            "request": {"detail": "full"},
        },
        "handoff_refs": {
            "available": bool(bundle.get("handoff_refs")),
            "count": len(bundle.get("handoff_refs") or []),
            "request": {"detail": "full"},
        },
        "project_context_refs": {
            "available": bool(bundle.get("project_context_refs")),
            "request": {"detail": "full"},
        },
        "context_pages": {
            "available": bool(full_payload.get("context_pages")),
            "count": int((full_payload.get("context_pages") or {}).get("count") or 0),
            "request": {"detail": "full"},
        },
        "resume_handoffs": {
            "available": bool(full_payload.get("resume_handoffs")),
            "count": len(full_payload.get("resume_handoffs") or []),
            "request": {"detail": "full"},
        },
        "next_actions": {
            "available": bool(full_payload.get("next_actions")),
            "count": len(full_payload.get("next_actions") or []),
            "request": {"detail": "full"},
        },
    }


def _project_pull_task_context_response(full_payload: dict[str, Any], *, detail: str, include_replay_bundle: bool, budget_args: dict[str, Any]) -> dict[str, Any]:
    if detail == "full" or include_replay_bundle:
        payload = dict(full_payload)
        payload["detail"] = "full"
        payload["available_layers"] = _layer_summary(full_payload)
        payload["token_budget"] = _estimate_response_tokens(payload, budget_args)
        payload["token_overhead"] = payload["token_budget"]
        return payload

    latest_checkpoint = dict(full_payload.get("latest_checkpoint") or {})
    latest_checkpoint.pop("raw_content", None)
    compact = {
        "project": full_payload.get("project"),
        "task_id": full_payload.get("task_id"),
        "status": full_payload.get("status"),
        "detail": "compact",
        "task": full_payload.get("task"),
        "latest_checkpoint": latest_checkpoint or None,
        "next_safe_action": full_payload.get("next_safe_action"),
        "replay_completeness": full_payload.get("replay_completeness"),
        "execution_readiness": full_payload.get("execution_readiness"),
        "replay_drill": full_payload.get("replay_drill"),
        "recommended_first_tool": full_payload.get("recommended_first_tool"),
        "task_statement_quality": full_payload.get("task_statement_quality"),
        "pending_capture_review_count": full_payload.get("pending_capture_review_count", 0),
        "promoted_capture_review_count": full_payload.get("promoted_capture_review_count", 0),
        "stenography_coverage": full_payload.get("stenography_coverage"),
        "available_layers": _layer_summary(full_payload),
    }
    compact_context_pages = _compact_task_context_pages(full_payload.get("context_pages") or {})
    if compact_context_pages:
        compact["context_pages"] = compact_context_pages
    compact["token_budget"] = _estimate_response_tokens(compact, budget_args)
    compact["token_overhead"] = compact["token_budget"]
    return compact


def _apply_completed_task_context_overlay(payload: dict[str, Any], *, project: str, task_id: str) -> None:
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    stored_status = str(task.get("status") or "").strip()
    if stored_status and stored_status != "done":
        task["stored_status"] = stored_status
    task["status"] = "done"
    payload["task"] = task
    payload["status"] = "done"
    payload["completion_evidence"] = {
        "source": "task_changes",
        "reason": "legacy_closeout_evidence",
    }
    payload["next_safe_action"] = "This task has closeout evidence; select the next priority open task instead of continuing it."
    payload["recommended_first_tool"] = "list_open_tasks"
    payload["execution_readiness"] = {
        "status": "not_applicable",
        "reason": "Task context contains closeout evidence; execution-readiness gaps must not reopen completed work.",
        "can_choose_next_action_without_user": True,
        "recommended_next_tool": "list_open_tasks",
        "recommended_next_action": payload["next_safe_action"],
    }
    payload["replay_drill"] = {
        "status": "done",
        "first_tool": "list_open_tasks",
        "first_action": payload["next_safe_action"],
        "tool_arguments": {"project": project},
        "rationale": "Completed task context is read-only closeout evidence, not an active execution bundle.",
        "blocking_missing": [],
        "evidence_used": ["task_changes.closeout_evidence"],
    }


async def build_pull_task_context_payload(api_base: str, args: dict[str, Any], *, dependencies: PullTaskContextDependencies) -> dict[str, Any]:
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args.get("task_id") or "").strip()
    limit = int(args.get("limit", 10))
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        detail = "compact"
    include_replay_bundle = bool(args.get("include_replay_bundle", False))
    budget_args = {
        "model_context_window": args.get("model_context_window"),
        "resume_budget_ratio": args.get("resume_budget_ratio"),
        "resume_budget_profile": str(args.get("resume_budget_profile") or "normal"),
    }
    selected_task = None
    if not task_id:
        listed = await dependencies.get(api_base, f"/artifacts?project={quote(project, safe='')}&status=open&type=task&limit={limit}")
        items = listed.get("items") or []
        selected_task = items[0] if items else None
        task_id = str((selected_task or {}).get("task_id") or "").strip()
    if not task_id:
        payload = {
            "project": project,
            "task_id": "",
            "status": "no_open_task",
            "next_safe_action": "Create or reopen a project task before continuing.",
        }
        payload["stenography_coverage"] = build_stenography_coverage(project=project, task_id=task_id)
        payload["stenography_protocol"] = build_stenography_protocol(project=project, task_id=task_id, state="task_context")
        payload.setdefault("project_identity", project_identity_envelope(requested_project=project, observed_project=project))
        payload["token_budget"] = _estimate_response_tokens(payload, budget_args)
        payload["token_overhead"] = payload["token_budget"]
        return payload

    # pull_task_context is read-only resume and must not start while the task is occupied.
    from app.services.task_lease_service import get_task_lease_store

    active_claim = get_task_lease_store().get_active_claim(project=project, task_id=task_id)
    if active_claim is not None:
        payload = {
            "project": project,
            "task_id": task_id,
            "status": "occupied",
            "occupied_by": {
                "owner_agent": active_claim.owner_agent,
                "owner_session_id": active_claim.session_id,
                "lease_id": active_claim.lease_id,
                "expires_at": active_claim.expires_at.isoformat(),
            },
            "next_safe_action": (
                "Task is occupied. Wait for lease release/expiry or coordinate handoff; "
                "do not start work from pull_task_context."
            ),
        }
        payload["stenography_coverage"] = build_stenography_coverage(project=project, task_id=task_id)
        payload["stenography_protocol"] = build_stenography_protocol(project=project, task_id=task_id, state="task_context")
        payload.setdefault("project_identity", project_identity_envelope(requested_project=project, observed_project=project))
        payload["token_budget"] = _estimate_response_tokens(payload, budget_args)
        payload["token_overhead"] = payload["token_budget"]
        return payload

    statement = await dependencies.get(api_base, f"/project/tasks/{quote(task_id, safe='')}/statement?project={quote(project, safe='')}")
    changes = await dependencies.get(api_base, f"/project/tasks/{quote(task_id, safe='')}/changes?project={quote(project, safe='')}&limit=100")
    has_closeout_evidence = task_has_closeout_evidence(changes or [])
    checkpoint_changes = [
        change for change in (changes or [])
        if "task_checkpoint" in {str(tag).strip() for tag in (change.get("tags") or [])}
        or "[task_checkpoint]" in str(change.get("content") or "")
    ]
    latest_checkpoint = _parse_task_checkpoint_change(checkpoint_changes[-1] if checkpoint_changes else None)
    next_actions = statement.get("next_actions") or []
    next_safe_action = (
        str((latest_checkpoint or {}).get("next_step") or "").strip()
        or str((next_actions[0] if next_actions else {}).get("action") or "").strip()
        or "Inspect the current task statement and record a planning checkpoint."
    )
    handoffs: list[dict[str, Any]] = []
    if bool(args.get("include_handoffs", True)):
        try:
            handoff_result = await dependencies.post(
                api_base,
                "/models/handoff/list",
                {
                    "agent_id": str(args.get("agent_id") or "codex").strip() or "codex",
                    "statuses": ["pending", "picked_up", "active", "paused"],
                    "limit": max(limit, 10),
                    "compact": True,
                },
            )
            handoffs = [
                item for item in (handoff_result.get("handoffs") or [])
                if str(item.get("task_id") or "").strip() == task_id
            ]
        except Exception:
            handoffs = []

    quality = statement.get("quality") or {}
    capture_review = statement.get("capture_review") or {}
    task = statement.get("task") or selected_task or {}
    linked_improvement = await _fetch_linked_improvement_bundle(api_base, project, str(task.get("linked_improvement_id") or ""), dependencies=dependencies)
    context_pages = _task_context_pages(project=project, task_id=task_id, include_content=True)
    payload = {
        "project": project,
        "task_id": task_id,
        "status": "ready",
        "task": {
            "title": task.get("title"),
            "status": task.get("status"),
            "linked_improvement_id": task.get("linked_improvement_id"),
        },
        "latest_checkpoint": latest_checkpoint,
        "next_safe_action": next_safe_action,
        "task_statement_quality": quality,
        "pending_capture_review_count": capture_review.get("pending_count", 0),
        "promoted_capture_review_count": capture_review.get("promoted_count", 0),
        "next_actions": next_actions[:5],
        "resume_handoffs": handoffs[:5],
        "recommended_first_tool": "record_task_checkpoint" if not latest_checkpoint else "pull_task_context",
    }
    if context_pages:
        payload["context_pages"] = context_pages
    payload["stenography_coverage"] = build_stenography_coverage(project=project, task_id=task_id)
    payload["stenography_protocol"] = build_stenography_protocol(project=project, task_id=task_id, state="task_context")
    payload.setdefault("project_identity", project_identity_envelope(requested_project=project, observed_project=project))
    payload["replay_bundle"] = _build_replay_bundle(
        project=project,
        task_id=task_id,
        statement=statement,
        changes=changes or [],
        handoffs=handoffs,
        linked_improvement=linked_improvement,
        context_pages=context_pages,
    )
    payload["replay_completeness"] = evaluate_replay_completeness(payload)
    payload["execution_readiness"] = evaluate_execution_readiness(payload)
    payload["replay_drill"] = build_replay_drill_decision(payload)
    if has_closeout_evidence:
        _apply_completed_task_context_overlay(payload, project=project, task_id=task_id)
    elif payload["replay_completeness"]["status"] == "incomplete":
        payload["recommended_first_tool"] = "record_task_checkpoint"
    elif payload["execution_readiness"]["status"] == "incomplete":
        payload["recommended_first_tool"] = "record_task_checkpoint"
    elif payload["replay_drill"]["status"] == "ready":
        payload["recommended_first_tool"] = payload["replay_drill"]["first_tool"]
    return _project_pull_task_context_response(payload, detail=detail, include_replay_bundle=include_replay_bundle, budget_args=budget_args)
