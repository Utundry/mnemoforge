"""
Super Memory MCP Server

Implements the Model Context Protocol (MCP) so any MCP-compatible LLM host
(Claude Desktop, Cursor, Continue, etc.) can use the memory server as a tool.

Transport: stdio (standard MCP transport)

Tools exposed:
  - memory_store        — save a new memory
  - memory_search       — semantic search
  - memory_get          — retrieve by ID
  - memory_delete       — delete by ID
  - memory_batch_store  — store multiple memories at once
  - memory_cleanup      — delete old / low-importance memories
  - memory_stats        — collection statistics
  - memory_health       — server health check
  - report_issue        — create an improvement/bug report
  - list_improvements   — list improvements
  - resolve_improvement — mark improvement resolved
  - ingest_file         — parse & ingest a local file
  - ingest_dir          — parse & ingest all files in a directory

Usage:
  python -m mcp.server

Configure in Claude Desktop (claude_desktop_config.json):
  {
    "mcpServers": {
      "super-memory": {
        "command": "python",
        "args": ["-m", "mcp.server"],
        "cwd": "D:/work/supermemory",
        "env": {"MEMORY_SERVER_URL": "http://localhost:8000"}
      }
    }
  }
"""

from __future__ import annotations

import json
import os
import sys
import logging
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

MEMORY_SERVER_URL = os.environ.get("MEMORY_SERVER_URL", "http://localhost:8000").rstrip("/")
API = f"{MEMORY_SERVER_URL}/api/v1"


# ── MCP protocol helpers ───────────────────────────────────────────────────────

def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _read() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line.strip())


def _ok(request_id: Any, content: list[dict]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": content, "isError": False},
    }


def _err(request_id: Any, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": f"Error: {message}"}],
            "isError": True,
        },
    }


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _http_post(path: str, payload: dict) -> dict:
    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{API}{path}", json=payload)
        r.raise_for_status()
        return r.json()


def _http_get(path: str) -> dict:
    with httpx.Client(timeout=30.0) as c:
        r = c.get(f"{API}{path}")
        r.raise_for_status()
        return r.json()


def _http_delete(path: str, payload: dict | None = None) -> dict | None:
    with httpx.Client(timeout=30.0) as c:
        if payload is not None:
            r = c.request("DELETE", f"{API}{path}", json=payload)
        else:
            r = c.delete(f"{API}{path}")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()


def _http_patch(path: str, payload: dict | None = None) -> dict:
    with httpx.Client(timeout=30.0) as c:
        if payload is not None:
            r = c.request("PATCH", f"{API}{path}", json=payload)
        else:
            r = c.request("PATCH", f"{API}{path}")
        r.raise_for_status()
        return r.json()


# ── Tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "supermemory_get_started",
        "description": (
            "CRITICAL ENTRY POINT: If you are an AI agent interacting with SuperMemory for the first time, "
            "or need to know how to use this system, call this tool IMMEDIATELY. "
            "It provides your core operating instructions, capabilities, and tells you where to find further guidance."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
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
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "preference", "experience", "task", "context"],
                    "default": "fact",
                },
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
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "preference", "experience", "task", "context"],
                },
                "category": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5},
                "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0},
            },
        },
    },
    {
        "name": "memory_get",
        "description": "Retrieve a specific memory by its UUID.",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "UUID of the memory"},
            },
        },
    },
    {
        "name": "memory_delete",
        "description": "Permanently delete a memory by its UUID.",
        "inputSchema": {
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "description": "UUID of the memory to delete"},
            },
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
        "name": "memory_health",
        "description": "Check if the memory server, Qdrant, and Ollama are all reachable.",
        "inputSchema": {"type": "object", "properties": {}},
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
        "name": "ingest_file",
        "description": (
            "Parse a local file (.md, .txt, .rst) into chunks and store each chunk as a memory. "
            "Markdown files are split by headings; text files by paragraphs. "
            "Front-matter tags are extracted automatically."
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
        "name": "ingest_dir",
        "description": (
            "Recursively scan a directory, parse all supported files, and store their contents as memories."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["path", "agent_id"],
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the directory"},
                "agent_id": {"type": "string"},
                "memory_type": {"type": "string", "default": "context"},
                "category": {"type": "string", "default": "document"},
                "importance_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by extensions, e.g. ['md', 'txt']. Empty = all supported.",
                    "default": [],
                },
                "recursive": {"type": "boolean", "default": True},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "fix_layout",
        "description": (
            "Detect and fix keyboard layout errors (Russian ↔ English) using rule-based + local LLM. "
            "Uses past corrections from memory as few-shot examples — improves over time. "
            "Returns corrected text, confidence, method used, and a correction_id for feedback."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "description": "Text to check and fix"},
                "force_llm": {"type": "boolean", "default": False, "description": "Force LLM even if rule is confident"},
                "agent_id": {"type": "string", "description": "Your agent ID for per-agent learning"},
            },
        },
    },
    {
        "name": "fix_layout_feedback",
        "description": (
            "Confirm or reject a previous layout fix. "
            "Teaches the system — confirmed fixes become few-shot examples for future corrections. "
            "Use correction_id from fix_layout response."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["correction_id", "confirmed"],
            "properties": {
                "correction_id": {"type": "string", "description": "ID from fix_layout response"},
                "confirmed": {"type": "boolean", "description": "True if fix was correct, False if wrong"},
                "correct_text": {"type": "string", "description": "The actual correct text (if confirmed=False)"},
            },
        },
    },
    {
        "name": "log_filter",
        "description": (
            "Filter a large log file/text using local LLM. "
            "Removes noise (debug, heartbeats, routine info), keeps errors/warnings/anomalies "
            "with surrounding context. Returns compressed log ready for cloud model analysis. "
            "Typical compression: 90-97% reduction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "log_text": {"type": "string", "description": "Raw log text to filter"},
                "file_path": {"type": "string", "description": "Absolute path to log file"},
                "focus": {"type": "string", "description": "Focus topic, e.g. 'memory errors', 'auth failures', 'HTTP 500'"},
                "context_lines": {"type": "integer", "default": 5, "description": "Lines of context around each kept line"},
                "use_llm": {"type": "boolean", "default": True, "description": "Use LLM for ambiguous lines"},
                "agent_id": {"type": "string", "default": "default"},
                "project_id": {"type": "string", "description": "Project name for per-project learning"},
            },
        },
    },
    {
        "name": "scan_ai_dirs",
        "description": (
            "Scan AI assistant directories (.claude, .codex, .continue, etc.) "
            "and ingest their contents into the vector memory. "
            "Parses conversation history, skills, settings, hooks. "
            "Skips already-ingested files (dedup by hash). "
            "Run once to bootstrap memory from existing AI history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Directories to scan. If empty — auto-detect AI dirs",
                },
                "agent_id": {"type": "string", "default": "ai-dirs"},
                "max_files": {"type": "integer", "default": 500},
                "dry_run": {"type": "boolean", "default": False, "description": "Report without storing"},
            },
        },
    },
    {
        "name": "watcher_start",
        "description": (
            "Start background file watcher on AI directories. "
            "New and modified files are automatically ingested into memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dirs": {"type": "array", "items": {"type": "string"}},
                "agent_id": {"type": "string", "default": "ai-dirs"},
            },
        },
    },
    {
        "name": "watcher_status",
        "description": "Get current file watcher status and list of watched directories.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── Tool execution ─────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> str:
    if name == "supermemory_get_started":
        return (
            "# SuperMemory Agent Guidelines\n\n"
            "Welcome! You are connected to SuperMemory, a semantic memory and self-improvement platform for AI agents.\n\n"
            "## 1. Core Workflow\n"
            "- **Proactive Preparation:** Prepare data and context so the user can make quick and easy decisions. Do not make critical decisions automatically.\n"
            "- **Safe Automation:** Automatic actions without permission are strictly limited to non-dangerous actions backed by repeating behavioral patterns.\n"
            "- **Context Gathering:** Whenever starting a complex task, use `memory_search` to find relevant past experiences or architectural decisions.\n"
            "- **Self-Healing (RepRap):** If you encounter bugs, missing features, or think of an enhancement, DO NOT ignore it. Use `report_issue` to log it.\n"
            "- **Layout Fixing:** If a user types Russian words on an English layout (e.g., 'kexit', 'z gsnfkcz'), use `fix_layout` to translate it.\n\n"
            "## 2. Where to find further instructions\n"
            "- **THE MAIN RULE:** All instructions and guidelines for SuperMemory MUST be searched for within SuperMemory itself.\n"
            "- **Open Tasks:** Call `list_improvements` with status='open' to see what needs to be built next.\n"
            "- **Project Knowledge:** Search the memory for 'architecture', 'docs', or 'components' to understand the project structure.\n"
            "- **Local Docs:** Read `README.md`, `plans/living_documentation.md`, and `scripts/improvements_report.md` in the workspace to sync with the current state.\n\n"
            "## 3. Your Goal\n"
            "You are a self-improving agent. Treat this memory as an extension of your own context window.\n\n"
            "**Action Required:** If you are starting a new session, run `list_improvements` now to see your pending tasks."
        )

    if name == "memory_store":
        data = _http_post("/memories", args)
        return f"Stored memory {data['id']}\n{json.dumps(data, indent=2, ensure_ascii=False)}"

    elif name == "memory_search":
        results = _http_post("/memories/search", args)
        if not results:
            return "No memories found."
        lines = []
        for r in results:
            m = r["memory"]
            lines.append(
                f"[{r['score']:.3f}] ({m['memory_type']}) {m['content'][:200]}\n  id={m['id']}"
            )
        return "\n\n".join(lines)

    elif name == "memory_get":
        data = _http_get(f"/memories/{args['memory_id']}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "memory_delete":
        _http_delete(f"/memories/{args['memory_id']}")
        return f"Deleted memory {args['memory_id']}"

    elif name == "memory_batch_store":
        # MCP clients sometimes serialize array args as JSON strings — normalise
        memories = args.get("memories", [])
        if isinstance(memories, str):
            memories = json.loads(memories)
        data = _http_post("/memories/batch", {"memories": memories})
        return f"Created {len(data['created_ids'])} memories. Failed: {data['failed_count']}"

    elif name == "memory_cleanup":
        data = _http_delete("/memories/cleanup", args)
        return f"Deleted {data['deleted_count']} memories."

    elif name == "memory_stats":
        data = _http_get("/stats")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "memory_health":
        data = _http_get("/health")
        return json.dumps(data, indent=2, ensure_ascii=False)

    elif name == "report_issue":
        data = _http_post("/improvements", args)
        return f"Improvement reported: {data['id']}\nTitle: {data['title']}\nStatus: {data['status']}"

    elif name == "list_improvements":
        project = args.get("project", "supermemory")
        status = args.get("status", "open")
        limit = args.get("limit", 20)
        params = urlencode({"project": project, "status": status, "limit": limit})
        results = _http_get(f"/improvements?{params}")
        if not results:
            return f"No {status} improvements for project '{project}'."
        lines = []
        for i, r in enumerate(results, 1):
            resolved = f" ✓ {r['resolved_at'][:10]}" if r.get("resolved_at") else ""
            lines.append(f"{i}. [{r['status']}]{resolved} {r['title']}\n   {r['description'][:120]}\n   id={r['id']}")
        return f"Improvements ({project}, {status}):\n\n" + "\n\n".join(lines)

    elif name == "resolve_improvement":
        data = _http_patch(f"/improvements/{args['improvement_id']}/resolve")
        return f"Resolved improvement {data['id']}"

    elif name == "ingest_file":
        data = _http_post("/ingest/file", args)
        return (
            f"File ingested: inserted={data['inserted']} "
            f"failed={data['failed']} skipped={data['skipped']}"
        )

    elif name == "ingest_dir":
        data = _http_post("/ingest/dir", args)
        return (
            f"Directory ingested: files={data['files_processed']} "
            f"inserted={data['inserted']} failed={data['failed']} skipped={data['skipped']}"
        )

    elif name == "fix_layout":
        data = _http_post("/layout/fix", args)
        lines = [
            f"method={data['method']}  confidence={data['confidence']}  correction_id={data['id']}",
        ]
        if data["was_fixed"]:
            lines += [
                f"direction: {data['direction']}",
                f"original:  {data['original']}",
                f"corrected: {data['corrected']}",
            ]
            if data.get("few_shot_count", 0) > 0:
                lines.append(f"(used {data['few_shot_count']} past examples)")
        else:
            lines.append(f"No fix needed: {data['original']}")
        return "\n".join(lines)

    elif name == "fix_layout_feedback":
        data = _http_post("/layout/feedback", args)
        status = "confirmed" if data["confirmed"] else "rejected"
        return f"Feedback recorded ({status}) for correction {data['correction_id']}"

    elif name == "log_filter":
        data = _http_post("/log/filter", args)
        ratio_pct = round((1 - data["compression_ratio"]) * 100, 1)
        header = (
            f"Log filtered: {data['original_lines']} -> {data['final_lines']} lines "
            f"({ratio_pct}% reduction)\n"
            f"  regex kept={data['stats']['kept_by_regex']}  "
            f"llm kept={data['stats']['kept_by_llm']}  "
            f"skipped={data['stats']['skipped_by_regex']}\n"
            f"{'-'*60}\n"
        )
        return header + data["filtered_log"]

    elif name == "scan_ai_dirs":
        if "dirs" in args and isinstance(args["dirs"], str):
            args["dirs"] = json.loads(args["dirs"])
        data = _http_post("/watcher/scan", args)
        cats = "  ".join(f"{k}={v}" for k, v in data.get("categories", {}).items())
        return (
            f"Scan complete:\n"
            f"  dirs:    {', '.join(data['scanned_dirs'])}\n"
            f"  files:   {data['files_processed']}\n"
            f"  chunks:  {data['chunks_found']} found, {data['chunks_stored']} stored, "
            f"{data['skipped_duplicates']} skipped (already in memory)\n"
            f"  categories: {cats}"
        )

    elif name == "watcher_start":
        if "dirs" in args and isinstance(args["dirs"], str):
            args["dirs"] = json.loads(args["dirs"])
        data = _http_post("/watcher/start", args)
        watched = "\n  ".join(data.get("watched_dirs", []))
        return f"Watcher {data['status']}:\n  {watched}"

    elif name == "watcher_status":
        data = _http_get("/watcher/status")
        watched = "\n  ".join(data.get("watched_dirs", []))
        return f"Watcher running={data['running']}\nWatched dirs:\n  {watched}"

    else:
        raise ValueError(f"Unknown tool: {name}")


# ── Main MCP loop ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    while True:
        msg = _read()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")

        # Initialization
        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "super-memory", "version": "1.0.0"},
                },
            })

        elif method == "initialized":
            pass  # notification, no response needed

        elif method == "tools/list":
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            })

        elif method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            try:
                result_text = execute_tool(tool_name, tool_args)
                _send(_ok(req_id, [_text(result_text)]))
            except httpx.HTTPStatusError as e:
                _send(_err(req_id, f"HTTP {e.response.status_code}: {e.response.text[:500]}"))
            except httpx.RequestError as e:
                _send(_err(req_id, f"Cannot connect to memory server: {e}"))
            except Exception as e:
                _send(_err(req_id, str(e)))

        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {}})

        else:
            if req_id is not None:
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })


if __name__ == "__main__":
    main()
