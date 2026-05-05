"""
Bootstrap endpoint for auto-configuring client machines.

  GET /client-setup             — Python script that sets up everything
  GET /client-setup/test        — connectivity test script
  GET /client-setup/mcp-server  — raw mcp/server.py file
  GET /client-setup/skills/remember  — SKILL.md for /remember
  GET /client-setup/skills/recall    — SKILL.md for /recall

Usage on a client machine (Linux/macOS):
  curl -s http://<SERVER_IP>:8000/client-setup | python3
  curl -s http://<SERVER_IP>:8000/client-setup/test | python3

Cross-platform Python fallback (avoids shell curl/IWR quirks):
  python -c "import urllib.request; exec(urllib.request.urlopen('http://<SERVER_IP>:8000/client-setup').read().decode('utf-8'))"
  python -c "import urllib.request; exec(urllib.request.urlopen('http://<SERVER_IP>:8000/client-setup/test').read().decode('utf-8'))"
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/client-setup")

# Locate project root relative to this file: app/routers/setup.py → ../../
_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _read(rel: str) -> str:
    return (_PROJECT_ROOT / rel).read_text(encoding="utf-8")


# ── Raw file endpoints ─────────────────────────────────────────────────────────

@router.get("/mcp-server", response_class=PlainTextResponse)
def get_mcp_server():
    """Serve mcp/server.py for download."""
    return _read("mcp/server.py")


@router.get("/test", response_class=PlainTextResponse)
def get_test(request: Request):
    """
    Return a self-contained connectivity test script.
    Tests: HTTP health, MCP SSE handshake, tools/list, memory_health tool call.
    """
    server_url = str(request.base_url).rstrip("/")

    script = f'''\
#!/usr/bin/env python3
"""
MnemoForge — client connectivity test.
Server: {server_url}

Checks:
  [1] HTTP /health
  [2] MCP SSE handshake (GET /mcp/sse → endpoint event)
  [3] MCP tools/list  (10 tools expected)
  [4] MCP tools/call  memory_health
  [5] MCP tools/call  memory_store + memory_search round-trip

Run:
  python3 test_client.py           (Linux/macOS)
  python  test_client.py           (Windows)
"""
import json
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error

SERVER = "{server_url}"
PASS = "\\033[32m[+]\\033[0m"
FAIL = "\\033[31m[-]\\033[0m"
WARN = "\\033[33m[!]\\033[0m"

results = []

def ok(label):
    results.append(True)
    print(f"  {{PASS}} {{label}}")

def fail(label, detail=""):
    results.append(False)
    suffix = f" — {{detail}}" if detail else ""
    print(f"  {{FAIL}} {{label}}{{suffix}}")

def fetch_json(url, data=None, method=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={{"Content-Type": "application/json"}},
        method=method or ("POST" if body else "GET"),
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# ── [1] HTTP health ────────────────────────────────────────────────────────────
print("\\n[1] HTTP health check")
try:
    h = fetch_json(f"{{SERVER}}/api/v1/health")
    qdrant = h.get("qdrant", {{}}).get("reachable")
    ollama = h.get("ollama", {{}}).get("reachable")
    if qdrant and ollama:
        ok(f"Server healthy (qdrant={{qdrant}} ollama={{ollama}})")
    else:
        fail(f"Partial health", f"qdrant={{qdrant}} ollama={{ollama}}")
except Exception as e:
    fail("Cannot reach server", str(e))
    print("\\nServer unreachable — aborting.")
    sys.exit(1)


# ── [2-4] MCP SSE protocol ────────────────────────────────────────────────────
# We open the SSE stream in a background thread, collect events,
# then send JSON-RPC requests to the received endpoint URL.

print("\\n[2] MCP SSE handshake")

import queue as Q

sse_queue   = Q.Queue()
endpoint_url = None
sse_error    = None

def _read_sse():
    """Keep SSE stream open; push every 'message' event to sse_queue."""
    global endpoint_url, sse_error
    try:
        req = urllib.request.Request(f"{{SERVER}}/mcp/sse")
        with urllib.request.urlopen(req, timeout=60) as r:
            event_type = None
            for raw in r:
                line = raw.decode("utf-8").rstrip("\\n\\r")
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event_type == "endpoint":
                        endpoint_url = data
                    elif event_type == "message":
                        sse_queue.put(json.loads(data))
    except Exception as e:
        sse_error = str(e)
        sse_queue.put(None)  # unblock any waiting mcp_call

t = threading.Thread(target=_read_sse, daemon=True)
t.start()

# Wait up to 5 s for the endpoint event
for _ in range(50):
    if endpoint_url:
        break
    time.sleep(0.1)

if endpoint_url:
    ok(f"SSE endpoint received: ...?sessionId={{endpoint_url.split('sessionId=')[-1][:8]}}...")
else:
    fail("SSE handshake failed", sse_error or "no endpoint event received")
    sys.exit(1)


def mcp_post(method, params=None):
    """POST a JSON-RPC request (returns 202, no body)."""
    payload = {{"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}}
    if params:
        payload["params"] = params
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        endpoint_url, data=body,
        headers={{"Content-Type": "application/json"}}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        pass  # 202 Accepted — no body

def mcp_call(method, params=None):
    """POST a JSON-RPC request; block until SSE delivers the response."""
    mcp_post(method, params)
    resp = sse_queue.get(timeout=10)           # wait for SSE response event
    if resp is None:
        raise RuntimeError("SSE stream closed")
    return resp


# ── [3] tools/list ────────────────────────────────────────────────────────────
print("\\n[3] MCP tools/list")
try:
    resp = mcp_call("tools/list")
    tools = resp.get("result", {{}}).get("tools", [])
    names = [t["name"] for t in tools]
    expected = {{"memory_store", "memory_search", "memory_get", "memory_health",
                 "memory_delete", "memory_batch_store", "memory_cleanup",
                 "memory_stats", "ingest_file", "ingest_dir"}}
    missing = expected - set(names)
    if not missing:
        ok(f"{{len(tools)}} tools registered: {{', '.join(sorted(names))}}")
    else:
        fail(f"Missing tools", ", ".join(missing))
except Exception as e:
    fail("tools/list failed", str(e))


# ── [4] memory_health tool call ───────────────────────────────────────────────
print("\\n[4] MCP tools/call — memory_health")
try:
    resp = mcp_call("tools/call", {{"name": "memory_health", "arguments": {{}}}})
    content = resp.get("result", {{}}).get("content", [])
    text = content[0].get("text", "") if content else ""
    data = json.loads(text) if text.startswith("{{") else {{}}
    if data.get("status") == "ok":
        ok(f"memory_health → status=ok")
    else:
        fail("memory_health returned unexpected result", text[:120])
except Exception as e:
    fail("memory_health call failed", str(e))


# ── [5] store + search round-trip ─────────────────────────────────────────────
print("\\n[5] MCP round-trip — store then search")
test_content = f"connectivity-test-{{uuid.uuid4().hex[:8]}}"
try:
    # store
    resp = mcp_call("tools/call", {{
        "name": "memory_store",
        "arguments": {{
            "content": test_content,
            "agent_id": "_test_client",
            "memory_type": "fact",
            "importance_score": 0.1,
        }}
    }})
    content = resp.get("result", {{}}).get("content", [])
    text = content[0].get("text", "") if content else ""
    mem_id = text.split("\\n")[0].replace("Stored memory ", "").strip() if "Stored memory" in text else None
    if mem_id:
        ok(f"memory_store → id={{mem_id[:8]}}...")
    else:
        fail("memory_store returned no ID", text[:80])
        mem_id = None

    # search
    resp = mcp_call("tools/call", {{
        "name": "memory_search",
        "arguments": {{"query": test_content, "agent_id": "_test_client", "limit": 1}}
    }})
    content = resp.get("result", {{}}).get("content", [])
    text = content[0].get("text", "") if content else ""
    if test_content[:20] in text:
        score_part = text.split("]")[0].replace("[", "").strip()
        ok(f"memory_search → found (score={{score_part}})")
    else:
        fail("memory_search didn't find stored memory", text[:80])

    # cleanup test entry
    if mem_id:
        mcp_call("tools/call", {{"name": "memory_delete", "arguments": {{"memory_id": mem_id}}}})

except Exception as e:
    fail("round-trip failed", str(e))


# ── Summary ────────────────────────────────────────────────────────────────────
passed = sum(results)
total  = len(results)
print("\\n" + "-"*40)
if passed == total:
    print(f"  \\033[32mAll {{passed}}/{{total}} checks passed\\033[0m  MCP client is working correctly.")
else:
    print(f"  \\033[31m{{passed}}/{{total}} checks passed\\033[0m  Fix the failures above.")
print()
'''
    return script


@router.get("/skills/remember", response_class=PlainTextResponse)
def get_skill_remember():
    return _read("skills/remember/SKILL.md")


@router.get("/skills/recall", response_class=PlainTextResponse)
def get_skill_recall():
    return _read("skills/recall/SKILL.md")


@router.get("/client-scan", response_class=PlainTextResponse)
def get_client_scan(request: Request):
    """
    Serve client_scan.py with server URL and Ollama URL pre-configured.
    Download and run on any client machine to sync local AI dirs to shared memory.
    """
    server_url = str(request.base_url).rstrip("/")
    # Derive Ollama URL: same host as the server, port 11434
    ollama_url = f"{request.base_url.scheme}://{request.base_url.hostname}:11434"
    script = _read("scripts/client_scan.py")
    script = re.sub(
        r'^DEFAULT_SERVER = .*$', 
        f'DEFAULT_SERVER = os.environ.get("MNEMOFORGE_SERVER_URL") or os.environ.get("SUPERMEMORY_SERVER_URL", "{server_url}")',
        script,
        flags=re.MULTILINE,
    )
    script = re.sub(
        r'^DEFAULT_OLLAMA = .*$', 
        f'DEFAULT_OLLAMA = os.environ.get("MNEMOFORGE_OLLAMA_URL") or os.environ.get("SUPERMEMORY_OLLAMA_URL", "{ollama_url}")',
        script,
        flags=re.MULTILINE,
    )
    return script


# ── Bootstrap script ───────────────────────────────────────────────────────────

@router.get("", response_class=PlainTextResponse)
@router.get("/", response_class=PlainTextResponse)
def get_bootstrap(request: Request):
    """
    Return a self-contained Python 3.9+ bootstrap script.
    The script auto-detects OS and sets up the MCP client.
    """
    server_url = str(request.base_url).rstrip("/")  # e.g. http://<SERVER_HOST>:8000

    script = f'''\
#!/usr/bin/env python3
"""
MnemoForge — client auto-setup script.
Generated by: {server_url}/client-setup

What this script does:
  1. Creates ~/mnemoforge-client/ directory
  2. Downloads mcp/server.py from the memory server
  3. Installs httpx into a local venv
  4. Registers the MCP server in Claude Code (user scope)
  5. Installs /remember and /recall skills
  6. Verifies the connection

Run with:
  python3 client_setup.py           (Linux/macOS)
  python  client_setup.py           (Windows)
"""
import os
import sys
import platform
import subprocess
import shutil
import json
import urllib.request
from pathlib import Path

SERVER_URL   = "{server_url}"
API_BASE     = f"{{SERVER_URL}}/api/v1"
SETUP_BASE   = f"{{SERVER_URL}}/client-setup"

IS_WINDOWS = platform.system() == "Windows"
HOME       = Path.home()
CLIENT_DIR = HOME / "mnemoforge-client"
SKILLS_DIR = HOME / ".claude" / "skills"


def log(msg: str):
    print(f"  {{msg}}")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode("utf-8")


def run(*cmd, check=True):
    subprocess.run(list(cmd), check=check)


# ── Step 1: health check ───────────────────────────────────────────────────────
print("\\n[1/5] Checking server availability...")
try:
    health = json.loads(fetch(f"{{API_BASE}}/health"))
    q = health.get("qdrant", {{}}).get("reachable")
    o = health.get("ollama", {{}}).get("reachable")
    if not q or not o:
        print(f"  WARNING: qdrant={{q}} ollama={{o}} — server may not be fully ready")
    else:
        log(f"Server healthy — qdrant={{q}} ollama={{o}}")
except Exception as e:
    print(f"  ERROR: Cannot reach {{SERVER_URL}} — {{e}}")
    print("  Make sure the memory server is running and reachable.")
    sys.exit(1)


# ── Step 2: download mcp/server.py ────────────────────────────────────────────
print("\\n[2/5] Downloading MCP server script...")
CLIENT_DIR.mkdir(parents=True, exist_ok=True)
server_py = CLIENT_DIR / "server.py"
server_py.write_text(fetch(f"{{SETUP_BASE}}/mcp-server"), encoding="utf-8")
log(f"Saved to {{server_py}}")


# ── Step 3: create venv + install httpx ───────────────────────────────────────
print("\\n[3/5] Setting up Python venv...")
venv_dir = CLIENT_DIR / ".venv"
if not venv_dir.exists():
    run(sys.executable, "-m", "venv", str(venv_dir))
    log("Created venv")
else:
    log("Venv already exists, skipping")

pip = str(venv_dir / ("Scripts/pip" if IS_WINDOWS else "bin/pip"))
python = str(venv_dir / ("Scripts/python" if IS_WINDOWS else "bin/python"))
run(pip, "install", "--quiet", "httpx")
log("httpx installed")


# ── Step 4: register MCP in Claude Code ───────────────────────────────────────
print("\\n[4/5] Registering MCP server in Claude Code...")

# Find claude binary
claude_candidates = []
if IS_WINDOWS:
    vscode_ext = HOME / ".vscode" / "extensions"
    if vscode_ext.exists():
        for p in vscode_ext.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"):
            claude_candidates.append(str(p))
    claude_candidates += ["claude.exe", "claude"]
else:
    claude_candidates = ["claude"]
    for p in ["/usr/local/bin/claude", str(HOME / ".local/bin/claude")]:
        claude_candidates.insert(0, p)

claude_bin = None
for c in claude_candidates:
    if shutil.which(c) or (Path(c).is_file() and os.access(c, os.X_OK)):
        claude_bin = c
        break

if claude_bin is None:
    print("  WARNING: claude binary not found — skipping MCP registration.")
    print(f"  Register manually:")
    print(f"    claude mcp add -s user -e MEMORY_SERVER_URL={{SERVER_URL}} mnemoforge -- {{python}} {{server_py}}")
else:
    # Remove existing entry if present (ignore errors)
    subprocess.run([claude_bin, "mcp", "remove", "mnemoforge", "-s", "user"],
                   capture_output=True)
    result = subprocess.run(
        [claude_bin, "mcp", "add", "-s", "user",
         "-e", f"MEMORY_SERVER_URL={{SERVER_URL}}",
         "--", python, str(server_py)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log("Registered as 'mnemoforge' (user scope)")
    else:
        # Old syntax fallback
        result2 = subprocess.run(
            [claude_bin, "mcp", "add", "-s", "user",
             "-e", f"MEMORY_SERVER_URL={{SERVER_URL}}",
             "mnemoforge",
             "--", python, str(server_py)],
            capture_output=True, text=True
        )
        if result2.returncode == 0:
            log("Registered as 'mnemoforge' (user scope)")
        else:
            print(f"  ERROR registering MCP: {{result.stderr.strip()}}")
            print(f"  Register manually:")
            print(f"    claude mcp add -s user -e MEMORY_SERVER_URL={{SERVER_URL}} -- {{python}} {{server_py}}")


# ── Step 5: install skills ─────────────────────────────────────────────────────
print("\\n[5/5] Installing /remember and /recall skills...")

for skill_name in ("remember", "recall"):
    skill_dir = SKILLS_DIR / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = fetch(f"{{SETUP_BASE}}/skills/{{skill_name}}")
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    log(f"/{{skill_name}} → {{skill_dir / 'SKILL.md'}}")


# ── Done ───────────────────────────────────────────────────────────────────────
print(f"""
Done! Restart VSCode/Claude Code to activate.

  Memory server : {{SERVER_URL}}
  MCP script    : {{server_py}}
  Skills        : /remember  /recall

Quick test (after restart):
  /remember This machine is connected to the shared memory server
  /recall shared memory
""")
'''
    return script
