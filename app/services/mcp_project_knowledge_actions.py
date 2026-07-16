"""Project knowledge and readiness MCP tool actions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.mcp_operational_rule_packet import build_operational_rule_packet
from app.services.mcp_pull_task_context import _checkpoint_stage_for_state, _operational_tray_target_tool
from app.services.mcp_tool_contracts import (
    build_enrich_task_payload,
    build_operational_tray_context_payload,
    build_project_bootstrap_payload,
    build_project_readiness_payload,
    build_project_reconstruction_payload,
    build_remote_snapshot_payload,
    build_task_execution_context_payload,
    build_upsert_knowledge_tree_node_payload,
    format_enrich_task_response,
    format_project_bootstrap_response,
    format_project_readiness_response,
    format_project_reconstruction_response,
    format_remote_snapshot_plan_response,
    format_remote_snapshot_sync_response,
    format_storage_trust_response,
)

GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
AnnotateCallback = Callable[[str, Any], Any]
ExecuteToolCallback = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ProjectKnowledgeActionDependencies:
    get: GetCallback
    post: PostCallback
    annotate_payload: AnnotateCallback
    execute_tool: ExecuteToolCallback


PROJECT_KNOWLEDGE_ACTIONS = {
    "search_project_knowledge",
    "enrich_task_with_context",
    "get_task_execution_context",
    "operational_tray",
    "upsert_knowledge_tree_node",
    "get_project_readiness",
    "get_project_bootstrap_checklist",
    "get_project_reconstruction_bundle",
    "plan_remote_snapshot",
    "sync_remote_snapshot",
    "get_storage_trust_status",
    "ingest_file",
    "ingest_dir",
}


async def execute_project_knowledge_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: ProjectKnowledgeActionDependencies,
) -> str:
    if name == "ingest_file":
        data = await dependencies.post(api_base, "/ingest/file", args)
        return f"File ingested: inserted={data['inserted']} failed={data['failed']} skipped={data['skipped']}"
    if name == "ingest_dir":
        data = await dependencies.post(api_base, "/ingest/dir", args)
        return (
            f"Directory ingested: files={data['files_processed']} "
            f"inserted={data['inserted']} failed={data['failed']} skipped={data['skipped']}"
        )
    if name == "search_project_knowledge":
        return await _search_project_knowledge(args=args, api_base=api_base, dependencies=dependencies)
    if name == "enrich_task_with_context":
        data = await dependencies.post(api_base, "/project/enrich-task", build_enrich_task_payload(args))
        return format_enrich_task_response(data)
    if name == "get_task_execution_context":
        data = await dependencies.post(api_base, "/task-execution-context", build_task_execution_context_payload(args))
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    if name == "operational_tray":
        return await _operational_tray(args=args, api_base=api_base, dependencies=dependencies)
    if name == "upsert_knowledge_tree_node":
        data = await dependencies.post(api_base, "/tree/upsert-by-path", build_upsert_knowledge_tree_node_payload(args))
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    if name == "get_project_readiness":
        data = await dependencies.post(api_base, "/project/readiness", build_project_readiness_payload(args))
        return format_project_readiness_response(data)
    if name == "get_project_bootstrap_checklist":
        data = await dependencies.post(api_base, "/project/bootstrap-checklist", build_project_bootstrap_payload(args))
        return format_project_bootstrap_response(data)
    if name == "get_project_reconstruction_bundle":
        data = await dependencies.post(api_base, "/project/reconstruction-bundle", build_project_reconstruction_payload(args))
        return format_project_reconstruction_response(data)
    if name == "plan_remote_snapshot":
        data = await dependencies.post(api_base, "/project/remote-snapshot/plan", build_remote_snapshot_payload(args))
        return format_remote_snapshot_plan_response(data)
    if name == "sync_remote_snapshot":
        data = await dependencies.post(api_base, "/project/remote-snapshot/sync", build_remote_snapshot_payload(args))
        return format_remote_snapshot_sync_response(data)
    if name == "get_storage_trust_status":
        data = await dependencies.get(api_base, "/admin/storage-trust")
        return format_storage_trust_response(data)
    raise ValueError(f"Unsupported project knowledge action: {name}")


async def _search_project_knowledge(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: ProjectKnowledgeActionDependencies,
) -> str:
    data = await dependencies.post(api_base, "/project/search", {
        "project_id": args["project_id"],
        "query": args["query"],
        "limit": args.get("limit", 5),
    })
    results = data.get("results", [])
    if not results:
        return (
            f"No components found for query '{args['query']}' in project '{args['project_id']}'.\n"
            "Run POST /project/ingest first to index the project."
        )
    lines = [f"Project '{args['project_id']}' - {len(results)} component(s) found:\n"]
    for result in results:
        lines.append(f"### {result['name']} ({result['component_id']})  score={result['score']}")
        lines.append(f"Purpose: {result['purpose']}")
        lines.append(f"Implementation: {result['implementation']}")
        if result.get("endpoints"):
            lines.append(f"Endpoints: {', '.join(result['endpoints'])}")
        if result.get("key_files"):
            lines.append(f"Key files: {', '.join(result['key_files'])}")
        if result.get("version_note"):
            lines.append(f"Note: {result['version_note']}")
        lines.append("")
    return "\n".join(lines)


async def _operational_tray(
    *,
    args: dict[str, Any],
    api_base: str,
    dependencies: ProjectKnowledgeActionDependencies,
) -> str:
    context_payload = build_operational_tray_context_payload(args)
    context = await dependencies.post(api_base, "/task-execution-context", context_payload)
    action = str(args.get("action") or "inspect").strip().lower()
    if action == "inspect":
        rule_packet = build_operational_rule_packet(context, args)
        data = {
            "project": context.get("project"),
            "state": context.get("state"),
            "task": context.get("task"),
            "readiness": context.get("readiness") or {},
            "operation_tray": context.get("operation_tray") or {},
            "required_rules": context.get("required_rules") or [],
            "recommended_rules": context.get("recommended_rules") or [],
            "rule_packet": rule_packet,
            "facade": {
                "allowed_actions": [
                    "record_stage_evidence",
                    "record_checkpoint",
                    "draft_checkpoint",
                    "review_rule_candidates",
                    "list_rule_candidates",
                ],
                "catalog_hidden": True,
            },
        }
        data = dependencies.annotate_payload("operational_tray", data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if action != "execute":
        raise ValueError("operational_tray action must be inspect or execute")

    tray_action = str(args.get("tray_action") or args.get("tool") or "").strip()
    action_args = args.get("args")
    if not isinstance(action_args, dict) or not action_args:
        action_args = args.get("arguments") or {}
    if not isinstance(action_args, dict):
        action_args = {}
    readiness = context.get("readiness") or {}
    ready_to_enter = bool(readiness.get("ready_to_enter", True))
    evidence_actions = {"record_stage_evidence", "record_checkpoint", "draft_checkpoint"}
    if not ready_to_enter and tray_action not in evidence_actions:
        data = {
            "blocked": True,
            "reason": "Readiness gate blocked the requested tray action.",
            "readiness": readiness,
            "operation_tray": context.get("operation_tray") or {},
            "next_allowed_actions": sorted(evidence_actions),
        }
        data = dependencies.annotate_payload("operational_tray", data)
        return json.dumps(data, indent=2, ensure_ascii=False)
    if bool(args.get("dry_run", False)):
        data = {
            "dry_run": True,
            "tray_action": tray_action,
            "readiness": readiness,
            "operation_tray": context.get("operation_tray") or {},
            "would_execute": _operational_tray_target_tool(tray_action),
        }
        data = dependencies.annotate_payload("operational_tray", data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    base_args = {
        "project": args.get("project", "mnemoforge"),
        "task_id": args.get("task_id", ""),
    }
    work_token = str(args.get("work_token") or "").strip()
    if tray_action == "record_stage_evidence":
        stage = str(action_args.get("stage") or _checkpoint_stage_for_state(str(args["state"]))).strip()
        checkpoint_args = {
            **base_args,
            **action_args,
            "stage": stage,
            "summary": action_args.get("summary") or f"Stage evidence recorded for {args['state']}.",
            "checkpoint_mode": action_args.get("checkpoint_mode") or "lightweight",
            "source": action_args.get("source") or "operational_tray",
            "acted_by": action_args.get("acted_by") or "codex",
            "session_id": action_args.get("session_id") or args.get("session_id", ""),
            "work_token": work_token,
        }
        return await dependencies.execute_tool("record_task_checkpoint", checkpoint_args)
    if tray_action == "record_checkpoint":
        checkpoint_args = {
            **base_args,
            **action_args,
            "stage": action_args.get("stage") or _checkpoint_stage_for_state(str(args["state"])),
            "summary": action_args.get("summary") or f"Checkpoint recorded for {args['state']}.",
            "source": action_args.get("source") or "operational_tray",
            "acted_by": action_args.get("acted_by") or "codex",
            "session_id": action_args.get("session_id") or args.get("session_id", ""),
            "work_token": work_token,
        }
        return await dependencies.execute_tool("record_task_checkpoint", checkpoint_args)
    if tray_action == "draft_checkpoint":
        draft_args = {
            **action_args,
            "project": args.get("project", "mnemoforge"),
            "task_id": args.get("task_id", action_args.get("task_id", "")),
            "work_id": action_args.get("work_id") or args.get("work_id", ""),
            "agent_id": action_args.get("agent_id") or args.get("agent_id", "codex"),
            "session_id": action_args.get("session_id") or args.get("session_id", ""),
            "task_title": action_args.get("task_title") or str(args.get("task") or "")[:160],
            "raw_notes": action_args.get("raw_notes") or str(args.get("task") or ""),
            "stage": action_args.get("stage") or _checkpoint_stage_for_state(str(args["state"])),
            "status": action_args.get("status") or "active",
        }
        return await dependencies.execute_tool("clerk_draft_report", draft_args)
    if tray_action == "review_rule_candidates":
        review_args = {
            "project": args.get("project", "mnemoforge"),
            "status": action_args.get("status", "candidate"),
            "source_task_id": action_args.get("source_task_id") or args.get("task_id", ""),
            "limit": action_args.get("limit", 100),
            "max_matches": action_args.get("max_matches", 5),
        }
        return await dependencies.execute_tool("get_rule_candidate_review_packet", review_args)
    if tray_action == "list_rule_candidates":
        list_args = {
            "project": args.get("project", "mnemoforge"),
            "status": action_args.get("status"),
            "source_task_id": action_args.get("source_task_id") or args.get("task_id", ""),
            "limit": action_args.get("limit", 100),
        }
        return await dependencies.execute_tool("list_rule_candidates", list_args)
    raise ValueError(f"Unsupported operational_tray tray_action: {tray_action}")


