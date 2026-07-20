"""Passive MCP session closeout recorder."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from app.services.mcp_tool_contracts import build_report_task_checkpoint_payload
from app.services.mcp_workflow_specs import load_named_json_spec, workflow_spec_cache

PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
DialogueTranscriptCallback = Callable[[list[dict[str, Any]]], str]


@workflow_spec_cache(maxsize=1)
def active_session_tools() -> set[str]:
    spec = load_named_json_spec("session/active_session_tools.json")
    return {str(item).strip() for item in spec.get("active_tools") or [] if str(item).strip()}


async def auto_record_session(
    ctx: dict[str, Any],
    *,
    post: PostCallback,
    build_dialogue_transcript: DialogueTranscriptCallback,
) -> None:
    """Auto-record a passive session observation when an SSE connection closes."""
    try:
        api_base = ctx.get("api_base", "")
        agent_id = ctx.get("agent_id", "default")
        pack_id = ctx.get("pack_id") or "auto"
        tools_called = {item["tool"] for item in ctx.get("tools_called", [])}
        skills_received = ctx.get("skills_received", [])
        checkpoint = ctx.get("current_task_checkpoint") or {}

        was_active = bool(tools_called & active_session_tools())
        skills_helpful = skills_received if was_active else []
        skills_unused = skills_received if not was_active else []
        duration_s = time.time() - ctx.get("connected_at", time.time())

        if checkpoint and not ctx.get("task_checkpoint_recorded"):
            project = str(checkpoint.get("project") or "").strip()
            task_id = str(checkpoint.get("task_id") or "").strip()
            stage = str(checkpoint.get("stage") or "").strip().lower()
            status = str(checkpoint.get("status") or "").strip().lower()
            summary = str(checkpoint.get("summary") or "").strip()
            blockers = checkpoint.get("blockers") or []
            next_step = str(checkpoint.get("next_step") or "").strip()
            reason = str(checkpoint.get("reason") or "").strip()
            if project and task_id and stage and summary:
                try:
                    await post(api_base, f"/project/tasks/{quote(task_id, safe='')}/changes", build_report_task_checkpoint_payload({
                        "project": project,
                        "task_id": task_id,
                        "stage": stage,
                        "status": status or None,
                        "summary": summary,
                        "blockers": blockers,
                        "next_step": next_step,
                        "reason": reason or "session_closed_auto_checkpoint",
                        "acted_by": agent_id,
                        "source": "mcp_session_close",
                    }))
                except Exception:
                    pass

        await post(api_base, "/skills/outcome", {
            "pack_id": pack_id,
            "agent_id": agent_id,
            "skills_helpful": skills_helpful,
            "skills_unused": skills_unused,
            "missing_domains": [],
            "success": was_active,
        })

        query_summary = "; ".join(ctx.get("queries", [])[:5])
        if query_summary:
            await post(api_base, "/memories", {
                "content": (
                    f"Agent {agent_id} session summary: "
                    f"searched for [{query_summary}], "
                    f"used {len(tools_called)} tools over {int(duration_s)}s"
                ),
                "agent_id": agent_id,
                "memory_type": "experience",
                "category": "session_observation",
                "importance_score": 0.5,
                "source": "auto-session-observer",
                "tags": ["session_observation", f"agent:{agent_id}"],
            })

        snippets = ctx.get("dialogue_snippets") or []
        if isinstance(snippets, list):
            transcript = build_dialogue_transcript([item for item in snippets if isinstance(item, dict)])
            if len(transcript.strip()) >= 60:
                await post(api_base, "/skills/dialogue/analyze", {
                    "transcript": transcript,
                    "agent_id": agent_id,
                    "session_id": ctx.get("session_id") or "",
                })
    except Exception:
        pass
