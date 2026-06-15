from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import uuid


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def _require(completed: subprocess.CompletedProcess[str], action: str) -> str:
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"{action} failed: {detail}")
    return completed.stdout.strip()


def _published_port(container_name: str) -> int:
    output = _require(
        _run(["docker", "port", container_name, "8000/tcp"]),
        "resolve published port",
    )
    endpoint = output.splitlines()[0].strip()
    try:
        return int(endpoint.rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"Unexpected docker port output: {output}") from exc


def _health(server: str, *, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{server}/api/v1/health", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_health(server: str, *, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            payload = _health(server)
            if str(payload.get("status") or "").lower() in {"ok", "healthy", "degraded"}:
                return payload
        except Exception as exc:
            last_error = str(exc)
        time.sleep(1.0)
    raise RuntimeError(f"Health did not become ready within {timeout_seconds:g}s: {last_error}")


def _validate_health(payload: dict) -> None:
    if payload.get("qdrant", {}).get("reachable") is not True:
        raise RuntimeError("First-run health reports Qdrant as unreachable.")
    if str(payload.get("status") or "").lower() == "degraded":
        providers = payload.get("llm_providers") or {}
        if providers.get("healthy") is not False or not providers.get("health_rule"):
            raise RuntimeError("Degraded first-run health does not explain unavailable LLM providers.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Black-box first-run audit for a production SloplessCode Docker image."
    )
    parser.add_argument("--image", required=True, help="Production image reference to start.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Readiness timeout in seconds.")
    args = parser.parse_args()

    container_name = f"sloplesscode-first-run-{uuid.uuid4().hex[:10]}"
    api_key = f"first-run-{uuid.uuid4().hex}"
    started = False
    try:
        _require(
            _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    container_name,
                    "-p",
                    "127.0.0.1::8000",
                    "-e",
                    "QDRANT_IN_MEMORY=true",
                    "-e",
                    f"API_KEY={api_key}",
                    args.image,
                ]
            ),
            "start production image",
        )
        started = True
        server = f"http://127.0.0.1:{_published_port(container_name)}"
        health = _wait_for_health(server, timeout_seconds=args.timeout)
        _validate_health(health)

        mounts = json.loads(
            _require(
                _run(["docker", "inspect", container_name, "--format", "{{json .Mounts}}"]),
                "inspect container mounts",
            )
            or "[]"
        )
        if mounts:
            raise RuntimeError(f"First-run container unexpectedly has mounts: {mounts}")

        smoke = _run(
            [
                sys.executable,
                "scripts/mcp_smoke.py",
                "--server",
                server,
                "--api-key",
                api_key,
            ]
        )
        if smoke.stdout:
            print(smoke.stdout.rstrip())
        _require(smoke, "MCP smoke")
        print(
            json.dumps(
                {
                    "ok": True,
                    "image": args.image,
                    "health_status": health.get("status"),
                    "qdrant_reachable": health.get("qdrant", {}).get("reachable"),
                    "llm_healthy": health.get("llm_providers", {}).get("healthy"),
                    "mounts": mounts,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"First-run audit failed: {exc}", file=sys.stderr)
        if started:
            logs = _run(["docker", "logs", container_name, "--tail", "120"])
            if logs.stdout:
                print(logs.stdout, file=sys.stderr)
            if logs.stderr:
                print(logs.stderr, file=sys.stderr)
        return 1
    finally:
        if started:
            _run(["docker", "stop", container_name])


if __name__ == "__main__":
    raise SystemExit(main())
