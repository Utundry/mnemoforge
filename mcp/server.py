#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

DEFAULT_SERVER_URL = "http://127.0.0.1:8000"


def _server_url() -> str:
    return (
        os.getenv("MEMORY_SERVER_URL")
        or os.getenv("MNEMOFORGE_SERVER_URL")
        or os.getenv("SUPERMEMORY_SERVER_URL")
        or os.getenv("SUPER_MEMORY_URL")
        or DEFAULT_SERVER_URL
    ).rstrip("/")


def _api_key() -> str:
    return (
        os.getenv("MEMORY_SERVER_API_KEY")
        or os.getenv("API_KEY")
        or os.getenv("MNEMOFORGE_API_KEY")
        or os.getenv("SUPER_MEMORY_API_KEY")
        or os.getenv("SUPERMEMORY_API_KEY")
        or ""
    ).strip()


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_error(req_id: Any, message: str) -> None:
    _emit(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": message},
        }
    )


def _post(client: httpx.Client, server: str, payload: Any) -> Any | None:
    resp = client.post(f"{server}/mcp/sse", json=payload)
    if resp.status_code == 202:
        return None
    resp.raise_for_status()
    raw = resp.text.strip()
    if not raw:
        return None
    return resp.json()


def main() -> int:
    server = _server_url()
    if not server:
        sys.stderr.write("MEMORY_SERVER_URL is required\n")
        return 1

    client = httpx.Client(timeout=30.0, headers=_headers())
    try:
        for line in sys.stdin:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"Invalid JSON input: {exc}\n")
                continue

            req_id = payload.get("id") if isinstance(payload, dict) else None
            try:
                result = _post(client, server, payload)
                if result is not None:
                    _emit(result)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else "error"
                body = exc.response.text.strip() if exc.response is not None else ""
                msg = f"HTTP {status}"
                if body:
                    msg += f": {body[:200]}"
                if req_id is not None:
                    _emit_error(req_id, msg)
                else:
                    sys.stderr.write(msg + "\n")
            except Exception as exc:
                if req_id is not None:
                    _emit_error(req_id, str(exc))
                else:
                    sys.stderr.write(f"{exc}\n")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
