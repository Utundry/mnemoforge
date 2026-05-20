from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CheckpointDraftActionDependencies:
    llm_gateway: Any
    qdrant: Any
    ollama: Any


def checkpoint_draft_recommended_next_tool(data: dict[str, Any]) -> str:
    status = str(data.get("status") or "").strip()
    if status == "approved":
        return "get_task_execution_context"
    if status in {"rejected", "expired"}:
        return "draft_checkpoint_from_spans"
    return (
        "approve_checkpoint_draft"
        if data.get("validation_report", {}).get("can_approve")
        else "revise_checkpoint_draft"
    )


async def execute_checkpoint_draft_action(
    *,
    name: str,
    args: dict[str, Any],
    dependencies: CheckpointDraftActionDependencies,
) -> dict[str, Any]:
    from app.services import checkpoint_draft_service as draft_service

    try:
        if name == "clerk_draft_report":
            data = await _clerk_draft_report(args=args, dependencies=dependencies)
        elif name == "draft_checkpoint_from_spans":
            data = (
                await draft_service.draft_checkpoint_from_spans(args, dependencies.llm_gateway)
            ).model_dump(mode="json")
            data["mutates_memory"] = False
            data["recommended_next_tool"] = checkpoint_draft_recommended_next_tool(data)
        elif name == "get_checkpoint_draft":
            data = _get_checkpoint_draft(args)
        elif name == "revise_checkpoint_draft":
            data = draft_service.revise_checkpoint_draft(
                str(args["draft_id"]),
                args.get("patch") or {},
                revised_by=str(args.get("revised_by") or "codex"),
            ).model_dump(mode="json")
            data["mutates_memory"] = False
            data["recommended_next_tool"] = checkpoint_draft_recommended_next_tool(data)
        elif name == "approve_checkpoint_draft":
            before_approve = draft_service.get_checkpoint_draft(
                str(args["draft_id"]),
                int(args["version"]),
            )
            was_approved = before_approve.status == "approved"
            data = (
                await draft_service.approve_checkpoint_draft(
                    str(args["draft_id"]),
                    int(args["version"]),
                    approved_by=str(args.get("approved_by") or "codex"),
                    qdrant=dependencies.qdrant,
                    ollama=dependencies.ollama,
                )
            ).model_dump(mode="json")
            data["mutates_memory"] = True
            data["saved_by_reference"] = True
            data["already_approved"] = was_approved
            data["recommended_next_tool"] = checkpoint_draft_recommended_next_tool(data)
        elif name == "reject_checkpoint_draft":
            data = draft_service.reject_checkpoint_draft(
                str(args["draft_id"]),
                int(args["version"]),
                rejected_by=str(args.get("rejected_by") or "codex"),
                reason=str(args.get("reason") or ""),
            ).model_dump(mode="json")
            data["mutates_memory"] = False
        else:
            raise ValueError(f"Unsupported checkpoint draft action: {name}")
        return data
    except draft_service.DraftValidationError as exc:
        return exc.to_dict()


async def _clerk_draft_report(
    *,
    args: dict[str, Any],
    dependencies: CheckpointDraftActionDependencies,
) -> dict[str, Any]:
    if str(args.get("raw_notes") or "").strip():
        from app.services import memory_scribe_service

        data = await memory_scribe_service.draft_task_checkpoint(
            {
                **args,
                "reason": str(args.get("reason") or "clerk_draft_report"),
            },
            dependencies.llm_gateway,
        )
        data["clerk_mode"] = "raw_notes"
        data["mutates_memory"] = False
        data["recommended_next_tool"] = (
            "record_task_checkpoint"
            if data.get("validation_report", {}).get("can_approve")
            else "revise_notes_or_add_evidence"
        )
        return data

    from app.services import checkpoint_draft_service as draft_service

    data = (
        await draft_service.draft_checkpoint_from_spans(
            {
                **args,
                "reason": str(args.get("reason") or "clerk_draft_report"),
                "preserve_evidence": bool(args.get("preserve_evidence", True)),
            },
            dependencies.llm_gateway,
        )
    ).model_dump(mode="json")
    data["clerk_mode"] = "stenographer_spans"
    data["mutates_memory"] = False
    data["recommended_next_tool"] = checkpoint_draft_recommended_next_tool(data)
    return data


def _get_checkpoint_draft(args: dict[str, Any]) -> dict[str, Any]:
    from app.services import checkpoint_draft_service as draft_service

    record = draft_service.get_checkpoint_draft(
        str(args["draft_id"]),
        int(args["version"]) if args.get("version") is not None else None,
    )
    data = record.model_dump(mode="json")
    if str(args.get("view") or "preview") == "preview":
        data = {
            "draft_id": data["draft_id"],
            "version": data["version"],
            "status": data["status"],
            "project": data["project"],
            "task_id": data["task_id"],
            "work_id": data["work_id"],
            "preview": data["preview"],
            "validation_report": data["validation_report"],
            "metrics": data["metrics"],
            "content_hash": data["content_hash"],
            "source_span_ids": data["source_span_ids"],
            "recommended_next_tool": checkpoint_draft_recommended_next_tool(data),
        }
    return data
