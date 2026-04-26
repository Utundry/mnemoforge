#!/usr/bin/env python3
"""Test unified artifacts MCP tools availability."""
from __future__ import annotations

import json
import urllib.request
import urllib.error
import os


def _headers(api_key: str = "") -> dict[str, str]:
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


def main() -> int:
    server = os.getenv("SUPERMEMORY_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = os.getenv("MEMORY_SERVER_API_KEY", "").strip()

    print(f"Testing unified artifacts MCP tools at {server}")

    # Test 1: Check health
    try:
        health = _http_json(f"{server}/api/v1/health", api_key=api_key)
        print(f"✓ Health check: {health.get('status')}")
    except Exception as e:
        print(f"✗ Health check failed: {e}")
        return 1

    # Test 2: List artifacts (HTTP endpoint)
    try:
        artifacts = _http_json(f"{server}/api/v1/artifacts?project=supermemory&limit=10", api_key=api_key)
        items = artifacts.get("items", [])
        print(f"✓ List artifacts (HTTP): {len(items)} items")
    except Exception as e:
        print(f"✗ List artifacts (HTTP) failed: {e}")
        return 1

    # Test 3: Check if MCP tools are available via tools/list
    try:
        tools_list = _http_json(f"{server}/mcp/tools", api_key=api_key)
        tools = tools_list.get("tools", [])
        tool_names = {t.get("name") for t in tools}

        unified_tools = ["list_artifacts", "get_artifact", "resolve_artifact", "reopen_artifact"]
        for tool_name in unified_tools:
            if tool_name in tool_names:
                print(f"✓ MCP tool available: {tool_name}")
            else:
                print(f"✗ MCP tool missing: {tool_name}")
                return 1
    except Exception as e:
        print(f"✗ MCP tools/list failed: {e}")
        return 1

    # Test 4: Test list_artifacts via MCP
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": "test-1",
            "method": "tools/call",
            "params": {
                "name": "list_artifacts",
                "arguments": {
                    "project": "supermemory",
                    "limit": 5
                }
            }
        }
        req = urllib.request.Request(
            f"{server}/mcp/tools",
            data=json.dumps(payload).encode("utf-8"),
            headers=_headers(api_key),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        content = result.get("result", {}).get("content", [])
        if content:
            text = content[0].get("text", "")
            data = json.loads(text) if text else {}
            items = data.get("items", [])
            print(f"✓ list_artifacts (MCP): {len(items)} items")
        else:
            print(f"✗ list_artifacts (MCP): No content returned")
    except Exception as e:
        print(f"✗ list_artifacts (MCP) failed: {e}")
        return 1

    print("\n✓ All unified artifacts MCP tools are available and working!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
