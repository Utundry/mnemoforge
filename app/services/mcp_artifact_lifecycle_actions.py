from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.mcp_tool_contracts import (
    build_approve_learning_candidate_payload,
    build_defer_learning_candidate_payload,
    build_list_artifacts_query,
    build_list_learning_candidates_query,
    build_merge_canonicals_payload,
    build_reconcile_completed_checkpoints_payload,
    build_reject_learning_candidate_payload,
    build_review_completed_checkpoint_scope_payload,
    build_review_completed_checkpoint_scopes_payload,
    build_set_canonical_status_payload,
    format_learning_candidate_transition,
    format_list_learning_candidates_response,
    format_set_canonical_status_response,
)


GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
PatchCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
AnnotatePayloadCallback = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ArtifactLifecycleActionDependencies:
    get: GetCallback
    post: PostCallback
    patch: PatchCallback
    annotate_payload: AnnotatePayloadCallback = lambda _name, data: data


async def execute_artifact_lifecycle_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: ArtifactLifecycleActionDependencies,
) -> str:
    if name == "review_improvement":
        improvement_id = args["improvement_id"]
        payload = {
            key: args[key]
            for key in ("stage", "verdict", "reviewed_by", "review_source", "reason")
            if args.get(key) is not None
        }
        data = await dependencies.patch(api_base, f"/improvements/{improvement_id}/review", payload)
        lines = [
            f"Improvement reviewed: {data['id']}",
            f"Title: {data['title']}",
            f"Stage: {data.get('stage') or 'proposal'}",
            f"Verdict: {data.get('verdict') or 'unset'}",
            f"Status: {data['status']}",
        ]
        return "\n".join(lines)

    if name == "list_learning_candidates":
        query = build_list_learning_candidates_query(args)
        data = await dependencies.get(api_base, f"/learning/artifacts?{query}")
        return format_list_learning_candidates_response(data)

    if name == "approve_learning_candidate":
        data = await dependencies.post(
            api_base,
            f"/learning/candidates/{args['artifact_id']}/approve",
            build_approve_learning_candidate_payload(args),
        )
        return format_learning_candidate_transition(data, action="approved")

    if name == "defer_learning_candidate":
        data = await dependencies.post(
            api_base,
            f"/learning/candidates/{args['artifact_id']}/defer",
            build_defer_learning_candidate_payload(args),
        )
        return format_learning_candidate_transition(data, action="deferred")

    if name == "reject_learning_candidate":
        data = await dependencies.post(
            api_base,
            f"/learning/candidates/{args['artifact_id']}/reject",
            build_reject_learning_candidate_payload(args),
        )
        return format_learning_candidate_transition(data, action="rejected")

    if name == "set_canonical_status":
        data = await dependencies.patch(
            api_base,
            f"/canonicals/{args['canonical_id']}/status",
            build_set_canonical_status_payload(args),
        )
        return format_set_canonical_status_response(data)

    if name == "merge_canonicals":
        data = await dependencies.post(
            api_base,
            f"/canonicals/{args['source_id']}/merge",
            build_merge_canonicals_payload(args),
        )
        return (
            f"Merged canonical {data['source_id']} -> {data['target_id']}\n"
            f"topic_path={data['topic_path']} supports={data['merged_support_count']}"
        )

    if name == "list_artifacts":
        query = build_list_artifacts_query(args)
        data = await dependencies.get(api_base, f"/artifacts?{query}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "get_artifact":
        artifact_key = args["artifact_key"]
        data = await dependencies.get(api_base, f"/artifacts/{quote(artifact_key, safe='')}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "reconcile_completed_checkpoints":
        payload = build_reconcile_completed_checkpoints_payload(args)
        data = await dependencies.post(api_base, "/artifacts/reconcile-completed-checkpoints", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "review_completed_checkpoint_scope":
        payload = build_review_completed_checkpoint_scope_payload(args)
        data = await dependencies.post(api_base, "/artifacts/completed-checkpoint-scope-review", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "review_completed_checkpoint_scopes":
        payload = build_review_completed_checkpoint_scopes_payload(args)
        data = await dependencies.post(api_base, "/artifacts/completed-checkpoint-scope-review/batch", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "resolve_artifact":
        artifact_key = args["artifact_key"]
        payload = {
            "acted_by": args.get("acted_by", "user"),
            "action_source": args.get("action_source", "inline_user_approval"),
            "reason": args.get("reason", ""),
        }
        data = await dependencies.post(api_base, f"/artifacts/{quote(artifact_key, safe='')}/resolve", payload)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "reopen_artifact":
        artifact_key = args["artifact_key"]
        project = args.get("project")
        if not project and ":" in artifact_key:
            project = artifact_key.split(":", 2)[1]
        payload = {
            "project": project,
            "status": args.get("status", "active"),
            "reason": args.get("reason", "reopen_artifact"),
            "acted_by": args.get("acted_by", "user"),
            "action_source": args.get("action_source", "unified_artifact"),
            "source": args.get("source", "unified-artifact"),
        }
        data = await dependencies.post(api_base, f"/artifacts/{quote(artifact_key, safe='')}/reopen", payload)
        return json.dumps(data, indent=2, ensure_ascii=False)

    raise ValueError(f"Unsupported artifact lifecycle action: {name}")
