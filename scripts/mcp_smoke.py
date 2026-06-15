#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _http_json(url: str, *, method: str = "GET", payload: dict | None = None, api_key: str = "", timeout: float = 12.0) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers=_headers(api_key),
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _sse_listener(server: str, out_queue: queue.Queue, stop_event: threading.Event, api_key: str = "") -> None:
    try:
        req = urllib.request.Request(f"{server}/mcp/sse", headers=_headers(api_key))
        with urllib.request.urlopen(req, timeout=60) as resp:
            current_event = ""
            while not stop_event.is_set():
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if current_event == "endpoint":
                        out_queue.put(("endpoint", data))
                    elif current_event == "message":
                        try:
                            out_queue.put(("message", json.loads(data)))
                        except json.JSONDecodeError:
                            out_queue.put(("message_raw", data))
    except Exception as exc:
        out_queue.put(("error", str(exc)))


def _mcp_call(endpoint_url: str, event_queue: queue.Queue, method: str, params: dict | None = None, api_key: str = "", timeout_s: float = 15.0) -> dict:
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(api_key),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            kind, data = event_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if kind == "message":
            return data
        if kind == "error":
            raise RuntimeError(f"SSE listener error: {data}")
    raise TimeoutError(f"Timed out waiting for MCP response: {method}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical SloplessCode HTTP+MCP smoke probe.")
    parser.add_argument(
        "--server",
        default=(os.getenv("MNEMOFORGE_SERVER_URL") or os.getenv("SUPERMEMORY_SERVER_URL") or "http://127.0.0.1:8000").rstrip("/"),
        help="Server base URL, e.g. http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--api-key",
        default=(
            os.getenv("MEMORY_SERVER_API_KEY")
            or os.getenv("API_KEY")
            or os.getenv("MNEMOFORGE_API_KEY")
            or os.getenv("SUPER_MEMORY_API_KEY")
            or os.getenv("SUPERMEMORY_API_KEY")
            or ""
        ).strip(),
        help="Optional API key (also read from env)",
    )
    args = parser.parse_args()

    ok = 0
    fail = 0

    def pass_step(msg: str) -> None:
        nonlocal ok
        ok += 1
        print(f"[PASS] {msg}")

    def fail_step(msg: str, detail: str = "") -> None:
        nonlocal fail
        fail += 1
        suffix = f" :: {detail}" if detail else ""
        print(f"[FAIL] {msg}{suffix}")

    print(f"Smoke probe target: {args.server}")

    try:
        health = _http_json(f"{args.server}/api/v1/health", api_key=args.api_key)
        status = str(health.get("status") or "").lower()
        if status in {"ok", "healthy", "degraded"}:
            pass_step(f"HTTP health status={status}")
        else:
            fail_step("HTTP health unexpected payload", json.dumps(health, ensure_ascii=False)[:200])
    except Exception as exc:
        fail_step("HTTP health request failed", str(exc))

    event_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    listener = threading.Thread(
        target=_sse_listener,
        args=(args.server, event_queue, stop_event, args.api_key),
        daemon=True,
    )
    listener.start()

    endpoint_url = ""
    deadline = time.time() + 8.0
    while time.time() < deadline and not endpoint_url:
        try:
            kind, data = event_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if kind == "endpoint":
            endpoint_url = str(data)
        elif kind == "error":
            fail_step("MCP SSE handshake failed", str(data))
            break

    if endpoint_url:
        pass_step("MCP SSE endpoint received")
    else:
        fail_step("MCP SSE endpoint not received in time")

    if endpoint_url:
        try:
            listed = _mcp_call(
                endpoint_url,
                event_queue,
                "tools/list",
                {"mode": "full"},
                api_key=args.api_key,
            )
            tools = listed.get("result", {}).get("tools", [])
            names = {tool.get("name") for tool in tools}
            if "memory_health" in names:
                pass_step(f"MCP tools/list returned {len(tools)} tools")
            else:
                fail_step("MCP tools/list missing memory_health")
        except Exception as exc:
            fail_step("MCP tools/list failed", str(exc))

        try:
            called = _mcp_call(
                endpoint_url,
                event_queue,
                "tools/call",
                {"name": "memory_health", "arguments": {}},
                api_key=args.api_key,
            )
            content = called.get("result", {}).get("content", [])
            text = content[0].get("text", "") if content else ""
            parsed = json.loads(text) if text.startswith("{") else {}
            health_status = str(parsed.get("status") or "").lower()
            if health_status in {"ok", "healthy", "degraded"}:
                pass_step(f"MCP tools/call memory_health returned status={health_status}")
            else:
                fail_step("MCP memory_health unexpected result", text[:200])
        except Exception as exc:
            fail_step("MCP tools/call memory_health failed", str(exc))

    stop_event.set()

    print(f"Summary: pass={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
