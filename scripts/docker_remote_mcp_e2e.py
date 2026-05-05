#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from scripts.testing_guard import assert_db_backed_test_target


SERVER = (os.getenv("MNEMOFORGE_SERVER_URL") or os.getenv("SUPERMEMORY_SERVER_URL", "http://memory-server-test:8000")).rstrip("/")
API_KEY = os.getenv("API_KEY", "test-api-key")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    return headers


def _request(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{SERVER}{path}",
        data=body,
        headers=_headers(),
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _mcp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
    if params is not None:
        payload["params"] = params
    result = _request("POST", "/mcp/sse", payload, timeout=60.0)
    if "error" in result:
        raise RuntimeError(json.dumps(result["error"], ensure_ascii=False))
    return result


def _mcp_text(tool_name: str, arguments: dict[str, Any]) -> str:
    result = _mcp("tools/call", {"name": tool_name, "arguments": arguments})
    content = result.get("result", {}).get("content") or []
    if not content:
        raise AssertionError(f"{tool_name} returned no content")
    return str(content[0].get("text") or "")


def _wait_health() -> None:
    deadline = time.time() + 90
    last_error = ""
    while time.time() < deadline:
        try:
            health = _request("GET", "/api/v1/health", timeout=10.0)
            if str(health.get("status") or "").lower() in {"ok", "healthy", "degraded"}:
                print(f"[PASS] health status={health.get('status')}")
                return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise TimeoutError(f"health did not become ready: {last_error}")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[PASS] {message}")


def main() -> int:
    print(f"Remote MCP e2e target: {SERVER}")
    assert_db_backed_test_target(SERVER, context="Remote MCP e2e")
    project = f"docker-e2e-{uuid.uuid4().hex[:8]}"

    try:
        _wait_health()

        listed = _mcp("tools/list")
        tools = {tool.get("name") for tool in listed.get("result", {}).get("tools", [])}
        for required in ("continue_task", "record_task_checkpoint", "enrich_task_with_context"):
            _assert(required in tools, f"tools/list contains {required}")

        improvement = _request(
            "POST",
            "/api/v1/improvements",
            {
                "title": "Docker remote MCP replay fixture",
                "description": "\n".join(
                    [
                        "Build an isolated Docker remote MCP replay fixture.",
                        "Assumption: test storage is separate from working MnemoForge data.",
                        "Constraint: client communicates over the Docker network.",
                        "Definition of done: replay drill executes its selected first tool.",
                    ]
                ),
                "project": project,
                "agent_id": "docker-e2e",
                "importance_score": 0.9,
                "tags": ["docker-e2e", "remote-mcp", "replay"],
            },
            timeout=60.0,
        )
        task_id = str(improvement["id"])
        _assert(bool(task_id), "created linked improvement/task fixture")

        for change_type, content in (
            ("decision", "Decision: remote MCP e2e must stay isolated from working storage."),
            ("implementation", "Implemented Docker test runner fixture over remote MCP."),
        ):
            _request(
                "POST",
                f"/api/v1/project/tasks/{task_id}/changes",
                {
                    "project": project,
                    "change_type": change_type,
                    "content": content,
                    "why": "The e2e test should validate realistic remote-agent behavior.",
                    "agent_id": "docker-e2e",
                    "source": "docker_remote_mcp_e2e",
                    "tags": ["docker-e2e", change_type],
                },
                timeout=60.0,
            )
        _assert(True, "recorded decision and implementation task changes")

        checkpoint_text = _mcp_text(
            "record_task_checkpoint",
            {
                "project": project,
                "task_id": task_id,
                "stage": "handoff",
                "status": "active",
                "summary": "Docker remote MCP fixture reached replay checkpoint.",
                "decisions": ["Use isolated Docker storage for e2e artifacts."],
                "changed_files": ["docker-compose.yml", "scripts/docker_remote_mcp_e2e.py"],
                "verification": ["Remote MCP e2e runner records and resumes this checkpoint."],
                "remaining_risk": ["This smoke depends on embedding service availability."],
                "next_step": "Invoke the replay drill selected first tool.",
                "acted_by": "docker-e2e",
                "to_agent": "docker-e2e",
            },
        )
        _assert("Checkpoint recorded" in checkpoint_text, "record_task_checkpoint recorded checkpoint")

        replay_text = _mcp_text(
            "continue_task",
            {
                "project": project,
                "task_id": task_id,
                "agent_id": "docker-e2e",
                "include_handoffs": True,
                "detail": "full",
                "limit": 10,
            },
        )
        replay = json.loads(replay_text)
        _assert(replay["replay_completeness"]["status"] == "complete", "continue_task replay completeness complete")
        _assert(replay["execution_readiness"]["status"] == "ready", "continue_task execution readiness ready")
        _assert(replay["replay_drill"]["status"] == "ready", "continue_task replay drill ready")

        first_tool = replay["replay_drill"]["first_tool"]
        first_args = replay["replay_drill"]["tool_arguments"]
        _assert(first_tool == "enrich_task_with_context", "replay drill selects enrich_task_with_context")
        context_text = _mcp_text(first_tool, first_args)
        _assert(bool(context_text.strip()), "replay drill selected tool returns non-empty context")
        _assert("Recommended MCP calls:" in context_text, "replay drill context includes recommended MCP calls")
        _assert("Available layers:" in context_text, "replay drill context exposes available layer index")
        _assert("Token budget:" in context_text, "replay drill context exposes token budget summary")

        enrich_compact = _request(
            "POST",
            "/api/v1/project/enrich-task",
            {
                **first_args,
                "detail": "compact",
                "model_context_window": 32000,
            },
            timeout=60.0,
        )
        _assert(enrich_compact.get("detail") == "compact", "handoff enrich defaults to compact detail")
        _assert(enrich_compact.get("context_profile") == "handoff_compact", "handoff enrich preserves context profile")
        _assert(bool(enrich_compact.get("available_layers")), "handoff enrich exposes available layers")
        enrich_budget = enrich_compact.get("token_budget") or {}
        _assert(enrich_budget.get("basis") == "model_context_window_ratio", "handoff enrich exposes ratio token budget")
        _assert(bool(enrich_budget.get("within_soft_limit")), "handoff enrich stays within soft token budget")

        enrich_full = _request(
            "POST",
            "/api/v1/project/enrich-task",
            {
                **first_args,
                "detail": "full",
                "model_context_window": 32000,
            },
            timeout=60.0,
        )
        _assert(enrich_full.get("detail") == "full", "handoff enrich full detail expands full layer")
        _assert(len(str(enrich_full.get("context") or "")) >= len(str(enrich_compact.get("context") or "")), "handoff enrich full context is not smaller than compact context")

        compact_text = _mcp_text(
            "continue_task",
            {
                "project": project,
                "task_id": task_id,
                "agent_id": "docker-e2e",
                "include_handoffs": True,
                "limit": 10,
            },
        )
        compact = json.loads(compact_text)
        _assert(compact.get("detail") == "compact", "continue_task defaults to compact detail")
        _assert("replay_bundle" not in compact, "compact continue_task omits replay_bundle")
        _assert(compact.get("available_layers", {}).get("task_history", {}).get("count", 0) >= 4, "compact continue_task exposes available layer index")
        overhead = compact.get("token_overhead") or {}
        _assert(bool(overhead.get("within_budget")), "compact continue_task stays within token overhead budget")

        print("[PASS] Docker remote MCP replay e2e completed")
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[FAIL] HTTP {exc.code}: {body}", file=sys.stderr)
    except Exception as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
