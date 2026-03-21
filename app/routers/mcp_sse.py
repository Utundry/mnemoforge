"""
MCP SSE Transport for FastAPI (spec 2024-11-05).

Allows zero-config client connection — no Python needed on the client:

    claude mcp add --transport sse -s user super-memory http://<SERVER_IP>:8000/mcp/sse

Protocol:
  GET  /mcp/sse                      — open SSE stream, receive endpoint URL
  POST /mcp/messages?sessionId=<id>  — send JSON-RPC requests
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

router = APIRouter(prefix="/mcp")
discovery_router = APIRouter()

import time

# Active SSE sessions: session_id → asyncio.Queue of response dicts
# Queues are in-process (tied to SSE stream) — cannot be stored externally.
_SESSIONS: dict[str, asyncio.Queue] = {}

_SSE_QUEUE_MAXSIZE = 200
_CLEANUP_INTERVAL_S = 60  # 1 minute
_cleanup_task = None


async def _touch_session(session_id: str) -> None:
    from app.services.mcp_session_store import get_session_store
    await get_session_store().touch(session_id)


async def _evict_expired_sessions() -> int:
    from app.services.mcp_session_store import get_session_store
    return await get_session_store().evict_expired()


def _ensure_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _loop():
        while True:
            try:
                await asyncio.sleep(_CLEANUP_INTERVAL_S)
                await _evict_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    _cleanup_task = loop.create_task(_loop())


async def _queue_put(queue: asyncio.Queue, msg: dict) -> None:
    try:
        if queue.full():
            try:
                queue.get_nowait()
            except Exception:
                pass
        queue.put_nowait(msg)
    except Exception:
        await queue.put(msg)


async def _session_observe(session_id: str | None, tool_name: str, extra: dict | None = None) -> None:
    """Record a tool call in the session context for passive tracking."""
    if not session_id:
        return
    from app.services.mcp_session_store import get_session_store
    store = get_session_store()
    patch: dict = {"tools_called": [{"tool": tool_name, "ts": time.time()}]}
    if tool_name in ("memory_search", "memory_context") and extra and extra.get("query"):
        patch["queries"] = [extra["query"]]
    elif tool_name in ("skill_search", "skill_install") and extra and extra.get("query"):
        patch["skills_accessed"] = [extra.get("query", "")]
    await store.patch_context(session_id, patch)


async def _mcp_live_observe(tool_name: str, args: dict, api_base: str) -> None:
    """
    MCP-agnostic server-side observer. Fires for every MCP client (Claude Code,
    Codex, Cline, Cursor, …) without requiring client-side hooks.

    - Emits mcp_tool_call event for observability
    - For memory_store / memory_search: updates project activity so decay gate
      doesn't penalise projects that write/search but never call memory_context
    """
    try:
        project = (
            args.get("context_project")
            or args.get("project")
            or ""
        )
        agent_id = args.get("agent_id") or "mcp-client"

        # Emit lightweight tool_call event (feeds dialogue analyzer + learning store)
        await _post(api_base, "/learning/events", {
            "event_type": "mcp_tool_call",
            "agent_id": agent_id,
            "project": project,
            "transport": "mcp",
            "episode_id": "",
            "context_signature": f"project={project};tool={tool_name};transport=mcp",
            "payload": {"tool_name": tool_name},
        })

        # For write/search tools: mark project as active so decay gate fires correctly
        # (memory_context already does this via /memories/context route internally)
        if tool_name in ("memory_store", "memory_search", "memory_batch_store") and project:
            from app.services.qdrant_service import get_qdrant_service
            qdrant = await get_qdrant_service()
            await qdrant.mark_used([], project=project)  # empty ids = just update activity ts

    except Exception:
        pass  # Never surface observer errors to MCP client


def _oauth_metadata(request: Request) -> dict[str, Any]:
    """Return benign OAuth discovery metadata for MCP clients that probe auth first."""
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": None,
        "token_endpoint": None,
        "registration_endpoint": None,
        "grant_types_supported": [],
        "response_types_supported": [],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": [],
    }


@discovery_router.get("/.well-known/oauth-authorization-server")
@discovery_router.get("/.well-known/oauth-authorization-server/mcp/sse")
@discovery_router.get("/mcp/sse/.well-known/oauth-authorization-server")
@discovery_router.get("/.well-known/oauth-protected-resource")
@discovery_router.get("/.well-known/oauth-protected-resource/mcp/sse")
async def oauth_authorization_server(request: Request) -> JSONResponse:
    """
    Return 404 so MCP clients skip OAuth and connect directly with API key.
    Claude Code (new versions) strictly validates OAuth metadata fields and
    rejects null values — 404 is the correct signal for "no OAuth required".
    """
    return JSONResponse(status_code=404, content={"detail": "OAuth not supported"})


# ── Tool definitions (mirrors mcp/server.py TOOLS) ────────────────────────────

TOOLS = [
    {
        "name": "memory_store",
        "description": (
            "Save a new memory to the semantic memory store. "
            "Use this to persist facts, preferences, experiences, tasks, or context "
            "so they can be retrieved later by semantic search."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["content", "agent_id"],
            "properties": {
                "content": {"type": "string", "description": "The memory text to store"},
                "agent_id": {"type": "string", "description": "Identifier of the agent/user who owns this memory"},
                "memory_type": {"type": "string", "enum": ["fact", "preference", "experience", "task", "context"], "default": "fact"},
                "category": {"type": "string", "default": "general"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "source": {"type": "string", "default": "conversation"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "memory_search",
        "description": (
            "Search the semantic memory store using natural language. "
            "Returns the most relevant memories sorted by a composite score "
            "(similarity + importance + recency)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "agent_id": {"type": "string", "description": "Filter by agent ID (optional)"},
                "memory_type": {"type": "string", "enum": ["fact", "preference", "experience", "task", "context"]},
                "category": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5},
                "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0},
                "since_minutes": {"type": "integer", "minimum": 1, "description": "Only memories added within last N minutes"},
            },
        },
    },
    {
        "name": "memory_context",
        "description": (
            "Build a model-ready context bundle from semantic memory search results. "
            "Returns a single text block plus a session_id that can be used with record_memory_outcome."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Natural language query for /memories/context"},
                "agent_id": {"type": "string", "description": "Agent ID (optional)"},
                "memory_type": {"type": "string", "enum": ["fact", "preference", "experience", "task", "context"]},
                "category": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0},
                "since_minutes": {"type": "integer", "minimum": 1, "description": "Only memories added within last N minutes"},
                "max_tokens": {"type": "integer", "minimum": 100, "maximum": 10000, "default": 2000},
                "format": {"type": "string", "enum": ["text", "markdown"], "default": "markdown"},
                "context_project": {"type": "string"},
                "context_file": {"type": "string"},
                "context_task_type": {"type": "string"},
                "session_id": {"type": "string", "description": "Optional episode/session id for outcome linking"},
            },
        },
    },
    {
        "name": "record_memory_outcome",
        "description": (
            "Record success/fail outcome for a /memories/context session and update importance scores "
            "of memories used in that episode."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["success"],
            "properties": {
                "success": {"type": "boolean", "description": "Whether the session outcome was successful"},
                "session_id": {"type": "string", "description": "Episode id returned by memory_context (preferred)"},
                "agent_id": {"type": "string", "description": "Agent identifier (optional)"},
                "project": {"type": "string", "description": "Project name (optional)"},
                "memory_ids": {"type": "array", "items": {"type": "string"}, "default": []},
                "boost": {"type": "number", "minimum": 0, "maximum": 1},
                "penalty": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
    {
        "name": "memory_recent",
        "description": "List memories added recently, sorted by time descending. Use to see what was saved in the last N minutes without needing a search query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 10, "description": "How many minutes back to look"},
                "agent_id": {"type": "string", "description": "Filter by agent ID (optional)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    {
        "name": "memory_get",
        "description": "Retrieve a specific memory by its UUID.",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {"memory_id": {"type": "string", "description": "UUID of the memory"}},
        },
    },
    {
        "name": "memory_delete",
        "description": "Permanently delete a memory by its UUID.",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {"memory_id": {"type": "string", "description": "UUID of the memory to delete"}},
        },
    },
    {
        "name": "memory_batch_store",
        "description": "Store multiple memories in a single request.",
        "inputSchema": {
            "type": "object",
            "required": ["memories"],
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["content", "agent_id"],
                        "properties": {
                            "content": {"type": "string"},
                            "agent_id": {"type": "string"},
                            "memory_type": {"type": "string"},
                            "category": {"type": "string"},
                            "importance_score": {"type": "number"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "minItems": 1,
                    "maxItems": 100,
                }
            },
        },
    },
    {
        "name": "memory_cleanup",
        "description": "Delete old and low-importance memories to free space.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "min_importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.2},
                "max_age_days": {"type": "integer", "minimum": 1, "default": 30},
            },
        },
    },
    {
        "name": "memory_stats",
        "description": "Get statistics about the memory collection (count, status, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "registry_best",
        "description": "Find the best component (local LLM, cloud LLM, skill) for a given task type. Use this to decide whether to handle a task locally or escalate to cloud.",
        "inputSchema": {
            "type": "object",
            "required": ["task_type"],
            "properties": {
                "task_type": {"type": "string", "description": "Task type: layout_fix, log_filter, fact_extraction, code_generation, code_review, text_summarization, skill_tagging, relevance_scoring, memory_extraction, query_expansion, architecture"},
                "exclude": {"type": "string", "description": "Comma-separated components to exclude"},
                "top": {"type": "integer", "default": 3},
            },
        },
    },
    {
        "name": "registry_update",
        "description": "Record a task outcome to update capability scores. Call after every LLM task to improve routing over time.",
        "inputSchema": {
            "type": "object",
            "required": ["component", "task_type", "success"],
            "properties": {
                "component": {"type": "string", "description": "e.g. 'qwen3:1.7b', 'cloud-llm', 'skill:fix-layout'"},
                "task_type": {"type": "string"},
                "success": {"type": "boolean"},
                "description": {"type": "string", "default": ""},
            },
        },
    },
    {
        "name": "registry_components",
        "description": "List all registered components with their capability scores per task type.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "crystallize_solution",
        "description": (
            "Convert a successful cloud LLM solution into a reusable skill (auto-publish). "
            "Call this after solving a task with cloud LLM when the solution is reusable. "
            "qwen3:1.7b will assess reusability, GLM will generate SKILL.md, and it publishes to marketplace automatically. "
            "Future identical tasks will be routed to the skill tier (instant/free). "
            "Use draft_skill instead if you want to review the SKILL.md before publishing."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task", "solution"],
            "properties": {
                "task": {"type": "string", "description": "The task description that was solved"},
                "solution": {"type": "string", "description": "The solution / procedure that worked"},
                "platform": {"type": "string", "default": "claude", "enum": ["claude", "codex", "cursor", "universal"]},
                "force": {"type": "boolean", "default": False, "description": "Crystallize even if reusability score is low"},
            },
        },
    },
    {
        "name": "draft_skill",
        "description": (
            "Three-stage pipeline: local LLM assesses → GLM drafts SKILL.md → YOU review (no auto-publish). "
            "Use this when you want to moderate the skill content before publishing. "
            "Returns draft SKILL.md and reusability score. "
            "After reviewing, call skill_publish with the (possibly edited) content, or discard if not useful."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task", "solution"],
            "properties": {
                "task": {"type": "string", "description": "The task description that was solved"},
                "solution": {"type": "string", "description": "The solution / procedure that worked"},
                "platform": {"type": "string", "default": "claude", "enum": ["claude", "codex", "cursor", "universal"]},
                "force": {"type": "boolean", "default": False, "description": "Generate draft even if reusability score is low"},
            },
        },
    },
    {
        "name": "route_task",
        "description": (
            "Classify a task and get routing recommendation: which component to use (local LLM, cached skill, or cloud LLM). "
            "Use this before expensive cloud LLM calls to check if local can handle it. "
            "After executing, record outcome with track_task."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task"],
            "properties": {
                "task": {"type": "string", "description": "Task description in natural language"},
                "task_type": {"type": "string", "description": "Override auto-classification"},
                "preferred_tier": {"type": "string", "enum": ["local", "cloud", "skill"], "description": "Force a specific tier"},
            },
        },
    },
    {
        "name": "track_task",
        "description": "Record a task execution outcome to the performance tracker. Call after every LLM task to build accurate capability data. If the task was misrouted (wrong task_type), set corrected_task_type to teach the dispatcher.",
        "inputSchema": {
            "type": "object",
            "required": ["component", "task_type", "success"],
            "properties": {
                "component": {"type": "string", "description": "'qwen3:1.7b', 'cloud-llm', 'skill:<name>'"},
                "task_type": {"type": "string"},
                "success": {"type": "boolean"},
                "latency_ms": {"type": "number"},
                "agent_id": {"type": "string"},
                "metadata": {"type": "object"},
                "corrected_task_type": {"type": "string", "description": "Set if the task was misclassified — the actual task type that should have been routed. Ivanov's feedback to Uncle Petya."},
            },
        },
    },
    {
        "name": "tracker_stats",
        "description": "Get aggregate performance statistics: success rates and latencies per component+task_type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "task_type": {"type": "string"},
                "since_hours": {"type": "number", "description": "Limit to last N hours"},
            },
        },
    },
    {
        "name": "report_issue",
        "description": (
            "Report a missing feature, incorrect behavior, or improvement idea encountered while working. "
            "Use this when you hit a limitation or bug in supermemory or any project. "
            "Saved improvements are reviewed during future development sessions."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "description"],
            "properties": {
                "title": {"type": "string", "description": "Short title of the issue or improvement"},
                "description": {"type": "string", "description": "Full description with context, steps to reproduce, expected behavior"},
                "project": {"type": "string", "default": "supermemory", "description": "Which project this applies to"},
                "agent_id": {"type": "string", "default": "llm", "description": "Who is reporting"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            },
        },
    },
    {
        "name": "list_improvements",
        "description": "List reported improvements and bugs for a project. Use at the start of a development session to see what needs to be fixed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "supermemory"},
                "status": {"type": "string", "enum": ["open", "resolved", "all"], "default": "open"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
        },
    },
    {
        "name": "improvements_report",
        "description": (
            "Generate a project status report: stats (total/open/resolved, top tags) "
            "and a GLM-written narrative summary with achievements and priorities. "
            "Use when you want a quick structured overview of project health."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "supermemory", "description": "Project name to report on"},
            },
        },
    },
    {
        "name": "knowledge_hierarchy",
        "description": (
            "Inspect canonical knowledge hierarchy grouped by scope. "
            "Returns domain/principle/meta canonicals, totals, and lifecycle counts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic_prefix": {"type": "string", "description": "Optional topic_path prefix filter"},
                "include_suppressed": {"type": "boolean", "default": False},
                "limit_per_scope": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
                "reconcile": {"type": "boolean", "default": False, "description": "Refresh canonical lifecycle before reading"},
            },
        },
    },
    {
        "name": "canonicals_by_scope",
        "description": "List canonical memories for one scope (domain, principle, or meta).",
        "inputSchema": {
            "type": "object",
            "required": ["scope"],
            "properties": {
                "scope": {"type": "string", "enum": ["domain", "principle", "meta"]},
                "topic_prefix": {"type": "string", "description": "Optional topic_path prefix filter"},
                "include_suppressed": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
    },
    {
        "name": "set_canonical_status",
        "description": "Suppress or reactivate a canonical memory for governance purposes.",
        "inputSchema": {
            "type": "object",
            "required": ["canonical_id", "suppressed"],
            "properties": {
                "canonical_id": {"type": "string"},
                "suppressed": {"type": "boolean"},
                "reason": {"type": "string"},
            },
        },
    },
    {
        "name": "merge_canonicals",
        "description": "Merge one canonical into another canonical of the same scope.",
        "inputSchema": {
            "type": "object",
            "required": ["source_id", "target_id"],
            "properties": {
                "source_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
        },
    },
    {
        "name": "skill_search",
        "description": (
            "Search the skill marketplace for relevant skills. "
            "Provide a context description to get LLM-filtered results relevant to your current task. "
            "Skills are filtered by domain (e.g. linuxcnc skills won't appear for web dev tasks)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {"type": "string", "description": "Current task/project context for smart filtering (e.g. 'building PyQt5 UI for CNC machine')"},
                "domains": {"type": "string", "description": "Comma-separated domain tags to filter by (e.g. 'web,deploy')"},
                "platform": {"type": "string", "enum": ["claude", "codex", "cursor", "universal"], "description": "Filter by platform"},
                "limit": {"type": "integer", "default": 10},
                "min_relevance": {"type": "number", "default": 0.3},
            },
        },
    },
    {
        "name": "skill_publish",
        "description": "Publish a skill to the shared marketplace. Domain tags are auto-extracted by LLM from the content.",
        "inputSchema": {
            "type": "object",
            "required": ["name", "content"],
            "properties": {
                "name": {"type": "string", "description": "Skill slug name"},
                "content": {"type": "string", "description": "Full SKILL.md content"},
                "platform": {"type": "string", "default": "claude"},
                "agent_id": {"type": "string", "default": "shared"},
                "domain_tags": {"type": "array", "items": {"type": "string"}, "description": "Override auto-detected tags"},
            },
        },
    },
    {
        "name": "skill_install",
        "description": "Get skill content by ID for local installation.",
        "inputSchema": {
            "type": "object",
            "required": ["skill_id"],
            "properties": {
                "skill_id": {"type": "string", "description": "UUID of the skill"},
            },
        },
    },
    {
        "name": "resolve_improvement",
        "description": "Mark a reported improvement or bug as resolved after implementing the fix.",
        "inputSchema": {
            "type": "object",
            "required": ["improvement_id"],
            "properties": {
                "improvement_id": {"type": "string", "description": "UUID of the improvement to mark as resolved"},
            },
        },
    },
    {
        "name": "memory_health",
        "description": "Check if the memory server, Qdrant, and Ollama are all reachable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "system_info",
        "description": (
            "Get a full overview of the supermemory system: what components exist, what each does, "
            "live counters (memories, skills, layout terms), active models, and infrastructure status. "
            "Call this when you want to understand what the system can do or need to explain it to the user."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_onboarding",
        "description": (
            "Get a personalized onboarding package based on accumulated experience from previous agents. "
            "Call this at the start of a session to receive relevant skills, behavioral patterns, "
            "domain gaps, and recent context — so you can hit the ground running without knowing the system. "
            "The system learns from each agent's session and passes that knowledge to you."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "description": "Your agent identifier (e.g. 'claude-code', 'codex')"},
                "task_description": {"type": "string", "description": "What you are working on — used to select relevant skills"},
            },
        },
    },
    {
        "name": "record_outcome",
        "description": (
            "Record what was helpful (or not) in your session. "
            "The system uses this to improve onboarding for future agents. "
            "Call at the end of a session or after completing a task."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "success"],
            "properties": {
                "agent_id": {"type": "string"},
                "pack_id": {"type": "string", "description": "Pack ID from get_onboarding (if any)"},
                "skills_helpful": {"type": "array", "items": {"type": "string"}, "description": "Skill IDs that helped"},
                "skills_unused": {"type": "array", "items": {"type": "string"}, "description": "Skill IDs that were irrelevant"},
                "missing_domains": {"type": "array", "items": {"type": "string"}, "description": "Knowledge areas that were missing"},
                "success": {"type": "boolean", "description": "Did the session accomplish its goal?"},
            },
        },
    },
    {
        "name": "ingest_file",
        "description": (
            "Parse a local file (.md, .txt, .rst) into chunks and store each chunk as a memory. "
            "Markdown files are split by headings; text files by paragraphs."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["path", "agent_id"],
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "agent_id": {"type": "string"},
                "memory_type": {"type": "string", "default": "context"},
                "category": {"type": "string", "default": "document"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "model_available",
        "description": (
            "List available cloud models ranked by remaining quota capacity. "
            "Use before starting a long task to pick the model with most remaining budget. "
            "Optionally filter by task_type to see models capable of specific tasks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string", "description": "Filter by task capability (optional)"},
            },
        },
    },
    {
        "name": "report_limit_hit",
        "description": (
            "Signal that a cloud model hit its rate/quota limit (429 or similar error). "
            "Triggers a cooldown period. Call this automatically when you receive a rate-limit error. "
            "After calling this, use model_available or route_task to find the next available model."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["model_id"],
            "properties": {
                "model_id": {"type": "string", "description": "e.g. 'claude-sonnet', 'gpt-4o'"},
                "error_code": {"type": "string", "description": "HTTP error code or API error code"},
                "error_msg": {"type": "string", "description": "Error message from the API"},
                "retry_after": {"type": "integer", "description": "Cooldown seconds (default 3600)"},
            },
        },
    },
    {
        "name": "handoff_task",
        "description": (
            "Package current task context in supermemory for pickup by another CLI tool. "
            "Use when: (1) current model hit its limit, (2) you want to manually switch to another CLI. "
            "Stores context with status=pending. Returns memory_id and pickup instruction for the target CLI."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["from_agent", "to_agent", "task_description"],
            "properties": {
                "from_agent": {"type": "string", "description": "This CLI: claude-code | codex | cline | gemini-cli"},
                "to_agent": {"type": "string", "description": "Target CLI: claude-code | codex | cline | gemini-cli"},
                "task_description": {"type": "string", "description": "Full task description to hand off"},
                "partial_result": {"type": "string", "description": "Any partial work done so far"},
                "key_facts": {"type": "array", "items": {"type": "string"}, "description": "Up to 10 key facts the next agent needs"},
                "task_id": {"type": "string", "description": "Task identifier (auto-generated if omitted)"},
                "reason": {"type": "string", "enum": ["manual", "limit_hit"], "default": "manual"},
            },
        },
    },
    {
        "name": "pickup_handoff",
        "description": (
            "Retrieve pending task handoffs addressed to this CLI agent. "
            "Call this at the start of a new session or when you expect a handoff from another CLI. "
            "Marks retrieved handoffs as picked_up to prevent double-processing."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "description": "This CLI's identity: claude-code | codex | cline | gemini-cli"},
                "limit": {"type": "integer", "default": 3, "description": "Max handoffs to retrieve"},
            },
        },
    },
    {
        "name": "ingest_dir",
        "description": "Recursively scan a directory, parse all supported files, and store their contents as memories.",
        "inputSchema": {
            "type": "object",
            "required": ["path", "agent_id"],
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the directory"},
                "agent_id": {"type": "string"},
                "memory_type": {"type": "string", "default": "context"},
                "category": {"type": "string", "default": "document"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "extensions": {"type": "array", "items": {"type": "string"}, "default": []},
                "recursive": {"type": "boolean", "default": True},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "search_project_knowledge",
        "description": (
            "Search the project knowledge cache for components relevant to a query. "
            "Returns component summaries (purpose, implementation, key files) without reading source code. "
            "Use this instead of grep/glob to understand what a component does. "
            "RepRap principle: the project documents itself so you don't start from scratch each session."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "query"],
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier, e.g. 'supermemory'"},
                "query": {"type": "string", "description": "Natural language query, e.g. 'layout fixer', 'skill crystallization'"},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
        },
    },
    {
        "name": "enrich_task_with_context",
        "description": (
            "Enrich a task description with relevant project component context. "
            "Call this at the start of a task to instantly get: which components are relevant, "
            "their purpose, implementation notes, and key files to look at. "
            "Replaces the grep → read → understand loop with a single call."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "task"],
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier"},
                "task": {"type": "string", "description": "Task description to enrich with context"},
                "max_components": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
            },
        },
    },
    {
        "name": "get_task_status",
        "description": (
            "Check the status of a background job submitted via ?background=true. "
            "Returns status (queued/running/done/failed) and result when complete. "
            "Use after submitting project_ingest, project_refresh, or skills_retag in background mode."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {"type": "string", "description": "Job ID returned by background submission"},
            },
        },
    },
]


# ── Async tool execution ───────────────────────────────────────────────────────

def _api_headers() -> dict:
    """Return auth headers for internal API calls."""
    from app.config import settings
    if settings.api_key:
        return {"X-Api-Key": settings.api_key}
    return {}


async def _post(api_base: str, path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=60.0, headers=_api_headers()) as c:
        r = await c.post(f"{api_base}{path}", json=payload)
        r.raise_for_status()
        return r.json()


async def _get(api_base: str, path: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as c:
        r = await c.get(f"{api_base}{path}")
        r.raise_for_status()
        return r.json()


async def _delete(api_base: str, path: str, payload: dict | None = None) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as c:
        if payload is not None:
            r = await c.request("DELETE", f"{api_base}{path}", json=payload)
        else:
            r = await c.delete(f"{api_base}{path}")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()


async def _patch(api_base: str, path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0, headers=_api_headers()) as c:
        r = await c.patch(f"{api_base}{path}", json=payload or {})
        r.raise_for_status()
        return r.json()


async def _execute_tool(name: str, args: dict, api_base: str, session_id: str | None = None) -> str:
    await _session_observe(session_id, name, args)

    # Server-side observer: works for any MCP client, no client hooks needed
    _OBSERVED_TOOLS = {"memory_store", "memory_search", "memory_context",
                       "memory_batch_store", "memory_delete", "record_memory_outcome"}
    if name in _OBSERVED_TOOLS:
        asyncio.create_task(_mcp_live_observe(name, args, api_base))

    if name == "memory_store":
        data = await _post(api_base, "/memories", args)
        return f"Stored memory {data['id']}\n{json.dumps(data, indent=2, ensure_ascii=False)}"

    elif name == "memory_search":
        results = await _post(api_base, "/memories/search", args)
        if not results:
            return "No memories found."
        lines = []
        for r in results:
            m = r["memory"]
            lines.append(f"[{r['score']:.3f}] ({m['memory_type']}) {m['content'][:200]}\n  id={m['id']}")
        return "\n\n".join(lines)

    elif name == "memory_context":
        data = await _post(api_base, "/memories/context", args)
        sid = data.get("session_id") or "—"
        ctx = (data.get("context") or "")
        snippet = ctx[:800]
        more = "…" if len(ctx) > len(snippet) else ""
        return (
            f"session_id={sid} used={data.get('used_count',0)} sources={data.get('source_count',0)} "
            f"scope_expanded={bool(data.get('scope_expanded'))}\n\n"
            f"{snippet}{more}"
        )

    elif name == "record_memory_outcome":
        data = await _post(api_base, "/outcomes", args)
        return (
            f"Recorded outcome: success={data.get('success')} session_id={data.get('session_id') or args.get('session_id')}\n"
            f"updated={data.get('updated',0)} skipped={data.get('skipped',0)}"
        )

    elif name == "memory_recent":
        params = f"?minutes={args.get('minutes', 10)}&limit={args.get('limit', 20)}"
        if args.get("agent_id"):
            params += f"&agent_id={args['agent_id']}"
        results = await _get(api_base, f"/memories/recent{params}")
        if not results:
            return "No recent memories found."
        lines = []
        for m in results:
            lines.append(f"[{m['timestamp'][:19]}] ({m['agent_id']}) {m['content'][:200]}\n  id={m['id']}")
        return "\n\n".join(lines)

    elif name == "memory_get":
        data = await _get(api_base, f"/memories/{args['memory_id']}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "memory_delete":
        await _delete(api_base, f"/memories/{args['memory_id']}")
        return f"Deleted memory {args['memory_id']}"

    elif name == "memory_batch_store":
        # MCP clients sometimes serialize array args as JSON strings — normalise
        import json as _json
        memories = args.get("memories", [])
        if isinstance(memories, str):
            memories = _json.loads(memories)
        data = await _post(api_base, "/memories/batch", {"memories": memories})
        return f"Created {len(data['created_ids'])} memories. Failed: {data['failed_count']}"

    elif name == "memory_cleanup":
        data = await _delete(api_base, "/memories/cleanup", args)
        return f"Deleted {data['deleted_count']} memories."

    elif name == "system_info":
        data = await _get(api_base, "/system/info")
        infra = data.get("infrastructure", {})
        counters = data.get("counters", {})
        components = data.get("components", [])
        models = infra.get("ollama", {}).get("models", [])

        lines = [
            f"supermemory — status: {data.get('status','?')} | uptime: {data.get('uptime_seconds',0)//60}m",
            f"Qdrant: {'✓' if infra.get('qdrant',{}).get('reachable') else '✗'}  "
            f"Ollama: {'✓' if infra.get('ollama',{}).get('reachable') else '✗'}  "
            f"embedding: {infra.get('embedding_model','?')} ({infra.get('embedding_dimensions','?')}d)",
            f"Models: {', '.join(models) or 'none'}",
            f"",
            f"Counters: memories={counters.get('memories',0)}  "
            f"skills={counters.get('skills',0)}  "
            f"layout_terms={counters.get('layout_terms',0)}",
            f"",
            f"Components ({len(components)}):",
        ]
        for c in components:
            tag = "[core]" if c.get("status") == "core" else "[opt] "
            lines.append(f"  {tag} {c['id']:20s} — {c['description'][:80]}")

        return "\n".join(lines)

    elif name == "memory_stats":
        data = await _get(api_base, "/stats")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "registry_best":
        params = f"task_type={args['task_type']}&top={args.get('top', 3)}"
        if args.get("exclude"):
            params += f"&exclude={args['exclude']}"
        data = await _get(api_base, f"/registry/best?{params}")
        lines = [f"Best components for '{data['task_type']}':"]
        for i, r in enumerate(data["ranked"], 1):
            bar = "█" * int(r["score"] * 10) + "░" * (10 - int(r["score"] * 10))
        lines.append(f"  {i}. {r['component']:20s} {bar} {r['score']:.3f}")
        return "\n".join(lines)

    elif name == "registry_update":
        data = await _post(api_base, "/registry/update", args)
        status = "✓" if args.get("success") else "✗"
        return f"{status} Updated {data['component']} / {data['task_type']} → score: {data['new_score']}"

    elif name == "registry_components":
        data = await _get(api_base, "/registry/components")
        lines = []
        for comp, caps in data.items():
            lines.append(f"\n{comp}:")
            for task, info in sorted(caps.items(), key=lambda x: -x[1]["score"]):
                bar = "█" * int(info["score"] * 10)
                lines.append(f"  {task:25s} {bar} {info['score']:.2f}  ({info['success']}✓/{info['fail']}✗)")
        return "\n".join(lines)

    elif name == "report_issue":
        data = await _post(api_base, "/improvements", args)
        return f"Improvement reported: {data['id']}\nTitle: {data['title']}\nStatus: {data['status']}"

    elif name == "list_improvements":
        project = args.get("project", "supermemory")
        status = args.get("status", "open")
        limit = args.get("limit", 20)
        results = await _get(api_base, f"/improvements?project={project}&status={status}&limit={limit}")
        if not results:
            return f"No {status} improvements for project '{project}'."
        lines = []
        for i, r in enumerate(results, 1):
            resolved = f" ✓ {r['resolved_at'][:10]}" if r.get("resolved_at") else ""
            lines.append(f"{i}. [{r['status']}]{resolved} {r['title']}\n   {r['description'][:120]}\n   id={r['id']}")
        return f"Improvements ({project}, {status}):\n\n" + "\n\n".join(lines)

    elif name == "improvements_report":
        project = args.get("project", "supermemory")
        data = await _get(api_base, f"/improvements/report?project={project}")
        s = data["stats"]
        lines = [
            f"## Project Status: {s['project']}",
            f"Total: {s['total']} | Resolved: {s['resolved']} ({s['resolved_pct']}%) | Open: {s['open']}",
            f"Top tags: {', '.join(t['tag'] for t in s['top_tags'])}",
        ]
        if s["top_open"]:
            lines.append("\n**Open (by importance):**")
            for item in s["top_open"]:
                lines.append(f"- [{item['importance']:.2f}] {item['title']}  id={item['id']}")
        if s["top_resolved"]:
            lines.append("\n**Top resolved:**")
            for item in s["top_resolved"]:
                lines.append(f"- [{item['importance']:.2f}] {item['title']}")
        if data.get("narrative"):
            lines.append("\n---\n")
            lines.append(data["narrative"])
        return "\n".join(lines)

    elif name == "knowledge_hierarchy":
        params = [
            f"include_suppressed={str(bool(args.get('include_suppressed', False))).lower()}",
            f"limit_per_scope={int(args.get('limit_per_scope', 25))}",
            f"reconcile={str(bool(args.get('reconcile', False))).lower()}",
        ]
        if args.get("topic_prefix"):
            params.append(f"topic_prefix={args['topic_prefix']}")
        data = await _get(api_base, f"/knowledge-hierarchy?{'&'.join(params)}")
        totals = data.get("totals", {})
        lifecycle = data.get("lifecycle", {})
        lines = [
            f"Knowledge hierarchy topic_prefix={data.get('topic_prefix') or 'all'}",
            f"domain={totals.get('domain',0)} principle={totals.get('principle',0)} meta={totals.get('meta',0)}",
            f"lifecycle: active={lifecycle.get('active',0)} suppressed={lifecycle.get('suppressed',0)} updated={lifecycle.get('updated',0)}",
        ]
        for scope in ("domain", "principle", "meta"):
            items = data.get("by_scope", {}).get(scope, [])
            if not items:
                continue
            lines.append(f"\n[{scope}]")
            for item in items[:10]:
                status = item.get("canonical_status") or ("suppressed" if item.get("suppressed") else "active")
                lines.append(
                    f"- {item.get('topic_path','?')} | supports={item.get('support_count',0)} | "
                    f"confidence={item.get('confidence',0):.2f} | status={status} | id={item.get('id')}"
                )
        return "\n".join(lines)

    elif name == "canonicals_by_scope":
        params = [
            f"scope={args['scope']}",
            f"include_suppressed={str(bool(args.get('include_suppressed', False))).lower()}",
            f"limit={int(args.get('limit', 50))}",
        ]
        if args.get("topic_prefix"):
            params.append(f"topic_prefix={args['topic_prefix']}")
        data = await _get(api_base, f"/canonicals/by-scope?{'&'.join(params)}")
        items = data.get("items", [])
        if not items:
            return f"No canonicals for scope '{args['scope']}'."
        lines = [f"Canonicals ({args['scope']}):"]
        for item in items:
            status = item.get("canonical_status") or ("suppressed" if item.get("suppressed") else "active")
            lines.append(
                f"- {item.get('topic_path','?')} | supports={item.get('support_count',0)} | "
                f"confidence={item.get('confidence',0):.2f} | status={status}\n  id={item.get('id')}"
            )
        return "\n".join(lines)

    elif name == "set_canonical_status":
        data = await _patch(
            api_base,
            f"/canonicals/{args['canonical_id']}/status",
            {"suppressed": args["suppressed"], "reason": args.get("reason")},
        )
        return (
            f"Canonical {data['id']} status={data['canonical_status']} "
            f"suppressed={bool(data.get('suppressed'))}"
        )

    elif name == "merge_canonicals":
        data = await _post(
            api_base,
            f"/canonicals/{args['source_id']}/merge",
            {"target_id": args["target_id"]},
        )
        return (
            f"Merged canonical {data['source_id']} → {data['target_id']}\n"
            f"topic_path={data['topic_path']} supports={data['merged_support_count']}"
        )

    elif name == "crystallize_solution":
        data = await _post(api_base, "/crystallizer/crystallize", args)
        if data["crystallized"]:
            return (
                f"✨ Skill crystallized: '{data['skill_name']}'\n"
                f"Score: {data['reusability_score']:.2f} | id: {data['skill_id']}\n"
                f"Reason: {data['reason']}\n"
                f"Next time this task is routed to skill tier (instant/free)."
            )
        else:
            return (
                f"⏭ Not crystallized (score {data['reusability_score']:.2f} < threshold)\n"
                f"Reason: {data['reason']}"
            )

    elif name == "draft_skill":
        data = await _post(api_base, "/crystallizer/draft", args)
        if not data["draft_ready"]:
            return (
                f"⏭ Draft not generated (score {data['reusability_score']:.2f} < threshold)\n"
                f"Reason: {data['reason']}"
            )
        publish_hint = (
            "✅ High score — recommended to publish as-is via skill_publish."
            if data.get("auto_publish_recommended")
            else "📝 Review the draft and edit if needed, then call skill_publish."
        )
        return (
            f"📋 Skill draft ready: '{data['skill_name']}'\n"
            f"Score: {data['reusability_score']:.2f} | {publish_hint}\n"
            f"Reason: {data['reason']}\n\n"
            f"--- SKILL.md draft ---\n{data['skill_content']}\n--- end draft ---\n\n"
            f"Call skill_publish(name='{data['skill_name']}', content=<above or edited>, "
            f"platform='{data.get('platform', 'claude')}') to publish."
        )

    elif name == "model_available":
        params = ""
        if args.get("task_type"):
            params = f"?task_type={args['task_type']}"
        models = await _get(api_base, f"/models/available{params}")
        if not models:
            return "No available cloud models. All models may be at quota or in cooldown."
        lines = ["Available cloud models:"]
        for m in models:
            bar = "█" * int(m["remaining_pct"] / 10) + "░" * (10 - int(m["remaining_pct"] / 10))
            lines.append(f"  {m['priority']}. {m['model_id']:15s} [{m['provider']}] {bar} {m['remaining_pct']:.0f}% remaining ({m['remaining']:,} {m['limit_unit']})")
        return "\n".join(lines)

    elif name == "report_limit_hit":
        data = await _post(api_base, "/models/report_limit", args)
        cooldown = data.get("cooldown_until")
        if cooldown:
            import time as _time
            secs = max(0, int(cooldown - _time.time()))
            return f"⛔ {args['model_id']} marked as rate-limited. Cooldown: {secs}s. Use model_available to find alternatives."
        return f"⛔ {args['model_id']} marked as rate-limited. Use model_available to find alternatives."

    elif name == "handoff_task":
        payload = {
            "from_agent": args["from_agent"],
            "to_agent": args["to_agent"],
            "task_description": args["task_description"],
            "partial_result": args.get("partial_result"),
            "key_facts": args.get("key_facts", []),
            "task_id": args.get("task_id"),
            "reason": args.get("reason", "manual"),
            "agent_id": "handoff",
        }
        data = await _post(api_base, "/models/handoff", payload)
        next_models = ", ".join(m["model_id"] for m in data.get("next_available", []))
        return (
            f"✅ Task packaged for handoff\n"
            f"task_id: {data['task_id']}\n"
            f"memory_id: {data['memory_id']}\n"
            f"To: {data['to_agent']}\n"
            f"Next available models: {next_models or 'none'}\n"
            f"Instruction: {data['pickup_instruction']}"
        )

    elif name == "pickup_handoff":
        data = await _post(api_base, "/models/handoff/pickup", args)
        if data["found"] == 0:
            return f"No pending handoffs for agent '{args['agent_id']}'."
        lines = [f"📥 Found {data['found']} pending handoff(s) for '{args['agent_id']}':"]
        for i, h in enumerate(data["handoffs"], 1):
            lines.append(f"\n--- Handoff {i} ---")
            lines.append(f"task_id: {h['task_id']}")
            lines.append(f"from: {h['from_agent']}")
            lines.append(f"memory_id: {h['memory_id']}")
            lines.append(h["content"][:800])
        return "\n".join(lines)

    elif name == "route_task":
        data = await _post(api_base, "/router/decide", args)
        tier_icon = {"skill": "⚡", "local": "🏠", "cloud": "☁️", "reference": "📞"}.get(data["tier"], "?")
        alts = ", ".join(f"{a['component']}({a['score']:.2f})" for a in data.get("alternatives", []))
        fallbacks = data.get("cloud_fallbacks", [])
        extra_str = ""
        if fallbacks and data["tier"] == "cloud":
            extra_str = "\nCloud fallbacks: " + ", ".join(f"{f['model_id']}({f['score']:.2f})" for f in fallbacks)
        references = data.get("references", [])
        if references and data["tier"] == "reference":
            ref_lines = "\n".join(
                f"  - {r['name']}: {r.get('description','')[:80]}"
                + (f"  → {r['reference_url']}" if r.get("reference_url") else "")
                for r in references
            )
            extra_str = f"\nReferences (pinned resources):\n{ref_lines}"
        return (
            f"{tier_icon} Route to: {data['component']} (tier={data['tier']}, score={data['score']:.2f})\n"
            f"Task type: {data['task_type']}\n"
            f"Reasoning: {data['reasoning']}\n"
            f"Alternatives: {alts or 'none'}"
            f"{extra_str}"
        )

    elif name == "track_task":
        data = await _post(api_base, "/tracker/record", args)
        status = "✓" if args.get("success") else "✗"
        note = f" → corrected to '{data['corrected_task_type']}'" if data.get("corrected_task_type") else ""
        return f"{status} Tracked {data['component']} / {data['task_type']}{note} (event #{data['event_id']})"

    elif name == "tracker_stats":
        params = []
        if args.get("component"):
            params.append(f"component={args['component']}")
        if args.get("task_type"):
            params.append(f"task_type={args['task_type']}")
        if args.get("since_hours"):
            params.append(f"since_hours={args['since_hours']}")
        qs = "?" + "&".join(params) if params else ""
        rows = await _get(api_base, f"/tracker/stats{qs}")
        if not rows:
            return "No performance data yet."
        lines = []
        for r in rows:
            bar = "█" * int(r["success_rate"] * 10)
            lat = f" {r['avg_latency_ms']:.0f}ms" if r["avg_latency_ms"] else ""
            lines.append(f"{r['component']:20s} / {r['task_type']:25s} {bar} {r['success_rate']:.2f}  ({r['success']}✓/{r['fail']}✗){lat}")
        return "\n".join(lines)

    elif name == "skill_search":
        params = []
        if args.get("context"):
            params.append(f"context={args['context']}")
        if args.get("domains"):
            params.append(f"domains={args['domains']}")
        if args.get("platform"):
            params.append(f"platform={args['platform']}")
        params.append(f"limit={args.get('limit', 10)}")
        params.append(f"min_relevance={args.get('min_relevance', 0.3)}")
        results = await _get(api_base, f"/skills/search?{'&'.join(params)}")
        if not results:
            return "No matching skills found."
        lines = []
        for i, s in enumerate(results, 1):
            tags = ", ".join(s.get("domain_tags", []))
            lines.append(f"{i}. [{s['platform']}] **{s['name']}** — {s['description']}\n   domains: {tags}\n   id: {s['id']}\n   install: {s['install_path']}")
        return "\n\n".join(lines)

    elif name == "skill_publish":
        data = await _post(api_base, "/skills/publish", args)
        return f"Published skill '{data['name']}'\nDomain tags: {data['domain_tags']}\nid: {data['id']}"

    elif name == "skill_install":
        data = await _get(api_base, f"/skills/{args['skill_id']}/content")
        return f"Skill: {data['name']}\nInstall to: {data['install_path']}\n\n--- SKILL.md ---\n{data['content']}"

    elif name == "resolve_improvement":
        data = await _patch(api_base, f"/improvements/{args['improvement_id']}/resolve")
        return f"Resolved improvement {data['id']}"

    elif name == "memory_health":
        data = await _get(api_base, "/health")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "ingest_file":
        data = await _post(api_base, "/ingest/file", args)
        return f"File ingested: inserted={data['inserted']} failed={data['failed']} skipped={data['skipped']}"

    elif name == "ingest_dir":
        data = await _post(api_base, "/ingest/dir", args)
        return (
            f"Directory ingested: files={data['files_processed']} "
            f"inserted={data['inserted']} failed={data['failed']} skipped={data['skipped']}"
        )

    elif name == "get_onboarding":
        agent_id = args.get("agent_id", "default")
        task_desc = args.get("task_description", "")

        sections: list[str] = []
        pack_id = ""

        # 0. Always prepend pinned references (the "phone on the wall")
        try:
            pinned = await _get(api_base, "/skills/pinned")
            if isinstance(pinned, list) and pinned:
                ref_skills = [s for s in pinned if s.get("reference_url")]
                plain_pinned = [s for s in pinned if not s.get("reference_url")]
                if ref_skills:
                    ref_lines = "\n".join(
                        f"  - {s['name']}: {s.get('description','')[:80]}  → {s['reference_url']}"
                        for s in ref_skills
                    )
                    sections.append(f"EMERGENCY REFERENCES (always available):\n{ref_lines}")
                if plain_pinned:
                    skill_lines = "\n".join(f"  - {s['name']}: {s.get('description','')[:80]}" for s in plain_pinned)
                    sections.append(f"PINNED SKILLS (always relevant):\n{skill_lines}")
        except Exception:
            pass

        # 0.5 Behavioral patterns ready for automation (conditional reflexes)
        try:
            from app.services.behavior_adaptation import iter_behavior_evals

            automatable = [
                p for p in iter_behavior_evals(agent_id)
                if p.suggest_automation and not p.high_risk
            ]
            if automatable:
                lines = []
                for p in automatable[:5]:
                    ctx = f" ({p.context_signature})" if p.context_signature else ""
                    lines.append(
                        f"  - {p.action_type}{ctx}: confidence={p.confidence} accepts={p.accepts}"
                    )
                sections.append(
                    "AUTOMATABLE HABITS (conditional reflexes):\n"
                    + "\n".join(lines)
                    + "\n\n"
                    "TIP: You may start doing these automatically. If the user opts out, "
                    "call POST /skills/behavior/patterns/{action_type}/reset."
                )
        except Exception:
            pass

        # 1. Determine domains: from task_description OR infer from history ("lost child" case)
        domains: list[str] = []
        inferred = False
        try:
            if task_desc:
                profile = await _post(api_base, "/skills/profile", {"text": task_desc})
                domains = profile.get("domains", []) or []
            else:
                # Agent said "I'm lost" — infer domain from history
                session_hints = []
                if session_id:
                    from app.services.mcp_session_store import get_session_store
                    _ctx = await get_session_store().get_context(session_id)
                    if _ctx:
                        session_hints = [t["tool"] for t in _ctx.get("tools_called", [])]
                inference = await _post(api_base, "/skills/infer-domain", {
                    "agent_id": agent_id, "session_hints": session_hints,
                })
                domains = inference.get("all_domains", []) or []
                top_domain = inference.get("domain", "general")
                confidence = inference.get("confidence", 0.0)
                signals = inference.get("signals", [])
                inferred = True
                if top_domain != "general":
                    sections.append(
                        f"ORIENTATION (inferred from your history):\n"
                        f"  You appear to be working in: {top_domain} (confidence: {confidence:.0%})\n"
                        f"  Evidence: {'; '.join(signals[:3])}"
                    )
                else:
                    sections.append(
                        "ORIENTATION: No prior history found — you are a new agent.\n"
                        "  Call record_outcome after your session to help future agents."
                    )
        except Exception as e:
            domains = ["general"]

        if not domains:
            domains = ["general"]

        # 2. Skill pack for inferred/provided domains
        try:
            pack = await _post(api_base, "/skills/pack/create", {
                "domains": domains, "task_type": "onboarding",
                "agent_id": agent_id, "confidence": 0.6, "limit": 5,
            })
            pack_id = pack.get("pack_id", "")
            skills = pack.get("skills", [])
            if session_id:
                from app.services.mcp_session_store import get_session_store
                await get_session_store().patch_context(session_id, {
                    "pack_id": pack_id,
                    "skills_received": [s.get("id", "") for s in skills],
                })

            label = "SKILLS FOR YOUR SESSION" if not inferred else f"SKILLS FOR DOMAIN '{domains[0]}'"
            if skills:
                skill_lines = "\n".join(f"  - {s['name']}: {s.get('description','')[:80]}" for s in skills)
                sections.append(f"{label} (pack_id={pack_id}):\n{skill_lines}")
            else:
                sections.append("No specific skills found yet — contribute outcomes to improve this.")
        except Exception as e:
            sections.append(f"Skills: unavailable ({e})")

        # 3. Domain gaps from collective experience
        try:
            gaps_data = await _get(api_base, f"/skills/gaps?agent_id={agent_id}&min_count=1")
            gaps = gaps_data.get("gaps", [])
            if gaps:
                gap_lines = ", ".join(g["domain"] for g in gaps[:5])
                sections.append(f"KNOWN KNOWLEDGE GAPS (from past sessions): {gap_lines}")
        except Exception:
            pass

        # 4. Analytics summary
        try:
            analytics = await _get(api_base, f"/skills/analytics?agent_id={agent_id}")
            total = analytics.get("total_outcomes", 0)
            rate = analytics.get("success_rate")
            if total > 0:
                sections.append(
                    f"COLLECTIVE EXPERIENCE: {total} past sessions, "
                    f"{int((rate or 0)*100)}% success rate"
                )
        except Exception:
            pass

        # 5. Recent memories for this agent type
        try:
            recent = await _get(api_base, f"/memories/recent?agent_id={agent_id}&limit=3&minutes=10080")
            if isinstance(recent, list) and recent:
                mem_lines = "\n".join(f"  - {m['content'][:100]}" for m in recent[:3])
                sections.append(f"RECENT CONTEXT FOR YOUR AGENT:\n{mem_lines}")
        except Exception:
            pass

        sections.append(
            "TIP: Call record_outcome at the end of your session to teach the system. "
            f"Use pack_id={pack_id!r} to reference this session's skill pack."
        )
        return "\n\n".join(sections)

    elif name == "record_outcome":
        data = await _post(api_base, "/skills/outcome", {
            "pack_id": args.get("pack_id", "manual"),
            "agent_id": args.get("agent_id", "default"),
            "skills_helpful": args.get("skills_helpful", []),
            "skills_unused": args.get("skills_unused", []),
            "missing_domains": args.get("missing_domains", []),
            "success": args.get("success", True),
        })
        return (
            f"Outcome recorded. Thank you — this improves onboarding for future agents.\n"
            f"report_id={data.get('report_id', '?')} success={data.get('stats', {}).get('success')}"
        )

    elif name == "search_project_knowledge":
        data = await _post(api_base, "/project/search", {
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
        lines = [f"Project '{args['project_id']}' — {len(results)} component(s) found:\n"]
        for r in results:
            lines.append(f"### {r['name']} ({r['component_id']})  score={r['score']}")
            lines.append(f"Purpose: {r['purpose']}")
            lines.append(f"Implementation: {r['implementation']}")
            if r.get("endpoints"):
                lines.append(f"Endpoints: {', '.join(r['endpoints'])}")
            if r.get("key_files"):
                lines.append(f"Key files: {', '.join(r['key_files'])}")
            if r.get("version_note"):
                lines.append(f"Note: {r['version_note']}")
            lines.append("")
        return "\n".join(lines)

    elif name == "enrich_task_with_context":
        data = await _post(api_base, "/project/enrich-task", {
            "project_id": args["project_id"],
            "task": args["task"],
            "max_components": args.get("max_components", 3),
        })
        if not data.get("components"):
            return data.get("message", "No relevant components found.")
        return data.get("context", "")

    elif name == "get_task_status":
        data = await _get(api_base, f"/tasks/{args['job_id']}")
        status = data.get("status", "unknown")
        job_type = data.get("job_type", "")
        lines = [f"Job {args['job_id'][:8]}… | type={job_type} | status={status}"]
        if status == "done":
            result = data.get("result") or {}
            lines.append(f"Result: {result}")
        elif status == "failed":
            lines.append(f"Error: {data.get('error', 'unknown error')}")
        elif status == "running":
            started = data.get("started_at")
            lines.append(f"Started at: {started}")
        return "\n".join(lines)

    else:
        raise ValueError(f"Unknown tool: {name}")


# ── JSON-RPC handler ───────────────────────────────────────────────────────────

def _ok(req_id: Any, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def _err(req_id: Any, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Error: {msg}"}], "isError": True}}


async def _auto_record_session(ctx: dict) -> None:
    """Auto-record a passive session observation when an SSE connection closes."""
    try:
        api_base = ctx.get("api_base", "")
        agent_id = ctx.get("agent_id", "default")
        pack_id = ctx.get("pack_id") or "auto"
        tools_called = {t["tool"] for t in ctx.get("tools_called", [])}
        skills_received = ctx.get("skills_received", [])

        # Infer which received skills were actually used (agent called related tools)
        used_tools = {"memory_store", "memory_search", "memory_context", "record_memory_outcome",
                      "ingest_file", "ingest_dir", "skill_search", "skill_install",
                      "crystallize_solution", "knowledge_hierarchy", "canonicals_by_scope",
                      "set_canonical_status", "merge_canonicals"}
        was_active = bool(tools_called & used_tools)

        # Skills that were received but agent didn't use any productive tools → unused
        skills_helpful = skills_received if was_active else []
        skills_unused = skills_received if not was_active else []

        duration_s = time.time() - ctx.get("connected_at", time.time())

        await _post(api_base, "/skills/outcome", {
            "pack_id": pack_id,
            "agent_id": agent_id,
            "skills_helpful": skills_helpful,
            "skills_unused": skills_unused,
            "missing_domains": [],
            "success": was_active,
        })

        # Also store session summary as a memory for future cross-agent recall
        query_summary = "; ".join(ctx.get("queries", [])[:5])
        if query_summary:
            await _post(api_base, "/memories", {
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
    except Exception:
        pass  # Never let observer errors surface


async def _handle(msg: dict, api_base: str, session_id: str | None = None) -> dict | None:
    method = msg.get("method", "")
    req_id = msg.get("id")

    if method == "initialize":
        # Extract agent identity from clientInfo
        client_info = msg.get("params", {}).get("clientInfo", {})
        agent_name = client_info.get("name", "") or ""
        # Normalise: "Claude Code" → "claude-code", "Codex CLI" → "codex"
        agent_id = agent_name.lower().replace(" ", "-") if agent_name else None

        if session_id and agent_id:
            from app.services.mcp_session_store import get_session_store
            await get_session_store().set_context(session_id, {
                "agent_id": agent_id,
                "connected_at": time.time(),
                "api_base": api_base,
                "tools_called": [],
                "queries": [],
                "skills_accessed": [],
                "skills_received": [],
                "pack_id": None,
            })

        result: dict = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "super-memory", "version": "1.0.0"},
        }
        if agent_id:
            result["_supermemory"] = {
                "agent_id": agent_id,
                "tip": "Call get_onboarding tool to receive skills and knowledge from past sessions.",
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    elif method in ("initialized", "notifications/initialized"):
        return None  # notification — no response

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    elif method == "tools/call":
        params = msg.get("params", {})
        try:
            result_text = await _execute_tool(
                params.get("name", ""), params.get("arguments", {}), api_base, session_id
            )
            return _ok(req_id, result_text)
        except httpx.HTTPStatusError as e:
            return _err(req_id, f"HTTP {e.response.status_code}: {e.response.text[:500]}")
        except httpx.RequestError as e:
            return _err(req_id, f"Cannot connect to memory server: {e}")
        except Exception as e:
            return _err(req_id, str(e))

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    else:
        if req_id is not None:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return None


# ── Streamable HTTP endpoint (MCP 2025-03-26, used by Codex CLI) ───────────────

@router.post("/sse")
async def streamable_http(request: Request):
    """MCP Streamable HTTP transport — accepts JSON-RPC, returns JSON directly."""
    base = str(request.base_url).rstrip("/")
    api_base = f"{base}/api/v1"

    body = await request.json()

    # Batch request (array of JSON-RPC objects)
    if isinstance(body, list):
        results = []
        for msg in body:
            r = await _handle(msg, api_base)
            if r is not None:
                results.append(r)
        return Response(
            content=json.dumps(results, ensure_ascii=False),
            media_type="application/json",
        )

    # Single request
    result = await _handle(body, api_base)
    if result is None:
        return Response(status_code=202)

    return Response(
        content=json.dumps(result, ensure_ascii=False),
        media_type="application/json",
    )


# ── SSE endpoints ──────────────────────────────────────────────────────────────

@router.get("/sse")
async def sse_connect(request: Request) -> StreamingResponse:
    """Open SSE stream. Server sends endpoint URL, then streams JSON-RPC responses."""
    _ensure_cleanup_task()
    await _evict_expired_sessions()
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
    _SESSIONS[session_id] = queue
    from app.services.mcp_session_store import get_session_store
    await get_session_store().init_session(session_id)

    # Build the POST endpoint URL using the same host/scheme the client used
    base = str(request.base_url).rstrip("/")
    endpoint = f"{base}/mcp/messages?sessionId={session_id}"

    async def stream():
        try:
            # Step 1: tell the client where to POST requests
            yield f"event: endpoint\ndata: {endpoint}\n\n"

            # Step 2: relay responses back over the stream
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    if msg is None:
                        break
                    yield f"event: message\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    await _touch_session(session_id)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # prevent proxy from closing idle connections
        finally:
            _SESSIONS.pop(session_id, None)
            from app.services.mcp_session_store import get_session_store
            ctx = await get_session_store().close_session(session_id)
            if ctx and ctx.get("tools_called"):
                # Auto-record passive session observation
                asyncio.create_task(_auto_record_session(ctx))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/messages")
async def sse_post(sessionId: str, request: Request) -> Response:
    """Receive a JSON-RPC message from the client and push the response to the SSE stream."""
    _ensure_cleanup_task()
    await _evict_expired_sessions()
    queue = _SESSIONS.get(sessionId)
    if queue is None:
        raise HTTPException(status_code=404, detail=f"Session {sessionId!r} not found or expired")

    # Build api_base so tools call back to this same server
    base = str(request.base_url).rstrip("/")
    api_base = f"{base}/api/v1"

    body = await request.json()
    await _touch_session(sessionId)
    result = await _handle(body, api_base, session_id=sessionId)
    if result is not None:
        await _queue_put(queue, result)
        await _touch_session(sessionId)

    return Response(status_code=202)
