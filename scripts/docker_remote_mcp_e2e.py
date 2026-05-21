#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def _mcp_json(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    text = _mcp_text(tool_name, arguments)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise AssertionError(f"{tool_name} returned a non-object JSON payload")
    return data


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
        for required in ("help", "state", "get", "submit"):
            _assert(required in tools, f"tools/list contains {required}")

        protocol_help = _mcp_json(
            "help",
            {
                "project": project,
                "detail": "brief",
                "runtime_profile_id": "weak_mcp_operator",
            },
        )
        _assert(protocol_help.get("simple_interface", {}).get("tools") == ["help", "state", "get", "submit"], "help exposes four-tool protocol")

        planning = _mcp_json(
            "state",
            {
                "project": project,
                "state": "planning",
                "runtime_profile_id": "weak_mcp_operator",
            },
        )
        planning_forms = {form.get("form_id") for form in planning.get("forms") or []}
        _assert({"create_improvement", "start_task", "store_memory"} <= planning_forms, "planning state exposes weak-model forms")

        memory = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "planning",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "store_memory",
                "payload": {
                    "project": project,
                    "content": "Docker remote MCP semantic get smoke fact for mailbox memory lookup.",
                    "category": "docker-e2e:simple-mailbox",
                    "memory_type": "context",
                    "importance_score": 0.2,
                    "tags": ["docker-e2e", "simple-get"],
                    "agent_id": "docker-e2e",
                },
            },
        )
        memory_id = str(memory.get("receipt", {}).get("id") or "")
        _assert(bool(memory_id), "submit store_memory returns memory id")

        semantic_get = _mcp_json(
            "get",
            {
                "project": project,
                "query": "Docker remote MCP semantic get smoke fact mailbox memory lookup",
                "runtime_profile_id": "weak_mcp_operator",
                "limit": 5,
            },
        )
        _assert(semantic_get.get("receipt", {}).get("resource_kind") == "memory_search", "get(query) routes semantic reads to memory_search")
        _assert(semantic_get.get("simple_interface", {}).get("route") == "memory_search", "get(query) exposes memory_search route")

        simple_improvement = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "planning",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "create_improvement",
                "payload": {
                    "project": project,
                    "title": "Docker remote MCP simple mailbox task",
                    "summary": "Validate public mailbox task lifecycle over remote MCP.",
                    "next_step": "Start, checkpoint, and finish through submit.",
                    "importance_score": 0.2,
                },
            },
        )
        simple_task_id = str(simple_improvement.get("receipt", {}).get("task_id") or "")
        _assert(bool(simple_task_id), "submit create_improvement returns task_id")

        started = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "planning",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "start_task",
                "payload": {
                    "project": project,
                    "task_id": simple_task_id,
                    "owner_agent": "docker-e2e",
                    "agent_fingerprint": f"docker-e2e:{project}",
                    "runtime_profile_id": "weak_mcp_operator",
                    "auto_heartbeat": False,
                    "summary": "Remote simple mailbox lifecycle started.",
                },
            },
        )
        work_token = str(started.get("receipt", {}).get("work_token") or "")
        _assert(started.get("receipt", {}).get("status") == "started", "submit start_task starts session")
        _assert(bool(work_token), "submit start_task returns work_token")

        progress = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "implementation",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "record_progress",
                "payload": {
                    "project": project,
                    "task_id": simple_task_id,
                    "owner_agent": "docker-e2e",
                    "work_token": work_token,
                    "summary": "Recorded simple mailbox remote lifecycle evidence.",
                    "changed_files": ["scripts/docker_remote_mcp_e2e.py"],
                    "verification": ["Remote simple mailbox progress accepted."],
                    "next_step": "No follow-up.",
                    "stage": "handoff",
                },
            },
        )
        _assert(progress.get("receipt", {}).get("status") == "accepted", "submit record_progress stores task evidence")

        finished = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "handoff",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "finish_task",
                "payload": {
                    "project": project,
                    "task_id": simple_task_id,
                    "owner_agent": "docker-e2e",
                    "work_token": work_token,
                    "summary": "Finished remote simple mailbox lifecycle without repeating progress evidence.",
                },
            },
        )
        _assert(finished.get("receipt", {}).get("status") == "finished", "submit finish_task finishes after record_progress evidence")

        improvement = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "planning",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "create_improvement",
                "payload": {
                    "project": project,
                    "title": "Docker remote MCP replay fixture",
                    "summary": "\n".join(
                        [
                            "Build an isolated Docker remote MCP replay fixture.",
                            "Assumption: test storage is separate from working MnemoForge data.",
                            "Constraint: client communicates over the Docker network.",
                            "Definition of done: replay drill executes its selected first tool.",
                        ]
                    ),
                    "next_step": "Record replay fixture task changes and checkpoint.",
                    "importance_score": 0.9,
                },
            },
        )
        task_id = str(improvement.get("receipt", {}).get("task_id") or "")
        _assert(bool(task_id), "created linked improvement/task fixture")

        replay_started = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "planning",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "start_task",
                "payload": {
                    "project": project,
                    "task_id": task_id,
                    "owner_agent": "docker-e2e",
                    "agent_fingerprint": f"docker-e2e-replay:{project}",
                    "runtime_profile_id": "weak_mcp_operator",
                    "auto_heartbeat": False,
                    "summary": "Remote replay fixture work started.",
                },
            },
        )
        replay_work_token = str(replay_started.get("receipt", {}).get("work_token") or "")
        _assert(bool(replay_work_token), "replay fixture start_task returns work_token")

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
                "work_token": replay_work_token,
            },
        )
        _assert("Checkpoint recorded" in checkpoint_text, "record_task_checkpoint recorded checkpoint")
        _assert("handoff_packet_created=True" in checkpoint_text, f"record_task_checkpoint created replay handoff text={checkpoint_text}")

        replay_released = _mcp_json(
            "submit",
            {
                "project": project,
                "state": "handoff",
                "runtime_profile_id": "weak_mcp_operator",
                "form_id": "release_task_claim",
                "payload": {
                    "project": project,
                    "task_id": task_id,
                    "owner_agent": "docker-e2e",
                    "reason": "Read replay context after checkpoint.",
                },
            },
        )
        _assert(replay_released.get("receipt", {}).get("status") == "released", "replay fixture releases claim before public read")

        replay_packet = _mcp_json(
            "get",
            {
                "ref": f"task:{project}:{task_id}",
                "project": project,
                "agent_id": "docker-e2e",
                "detail": "full",
                "runtime_profile_id": "weak_mcp_operator",
                "limit": 10,
            },
        )
        replay = replay_packet["result"]
        _assert("replay_completeness" in replay, f"get(task ref, detail=full) exposes replay fields keys={sorted(replay.keys())}")
        _assert(replay["replay_completeness"]["status"] == "complete", "continue_task replay completeness complete")
        readiness = replay["execution_readiness"]
        _assert(
            readiness["status"] == "ready",
            f"continue_task execution readiness ready status={readiness['status']} missing={readiness.get('missing_evidence')}",
        )
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

        compact_packet = _mcp_json(
            "get",
            {
                "ref": f"task:{project}:{task_id}",
                "project": project,
                "agent_id": "docker-e2e",
                "runtime_profile_id": "weak_mcp_operator",
                "limit": 10,
            },
        )
        compact = compact_packet["result"]
        _assert(compact_packet.get("simple_interface", {}).get("mode") == "ref", "get(task ref) uses public ref mode")
        _assert("replay_bundle" not in compact, "compact continue_task omits replay_bundle")
        _assert(compact.get("task_id") == task_id, "compact get(task ref) keeps task identity")
        _assert(compact.get("latest_checkpoint", {}).get("summary"), "compact get(task ref) keeps latest checkpoint summary")

        print("[PASS] Docker remote MCP simple mailbox and replay e2e completed")
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[FAIL] HTTP {exc.code}: {body}", file=sys.stderr)
    except Exception as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
