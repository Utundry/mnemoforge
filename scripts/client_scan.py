#!/usr/bin/env python3
"""
SuperMemory Client Scanner
==========================
Scans local AI assistant directories and ingests their content
into the shared vector memory server.

All heavy lifting (summarization, classification) is done by the LOCAL LLM
via Ollama — no expensive cloud tokens used.

Usage:
    python client_scan.py [options]

Options:
    --server  <SERVER_URL>                Memory server URL
    --ollama  <OLLAMA_URL>                Ollama URL (local or network)
    --agent   my-machine                  Agent ID (default: hostname)
    --model   qwen3:1.7b                  Ollama model for summarization
    --dry-run                             Parse but don't send
    --force                               Re-ingest even unchanged files
    --dirs    path1 path2                 Override directories to scan
    --no-llm                              Skip LLM, use fast rule-based parsing
    --verbose                             Verbose output
    --api-key  <API_KEY>                  API key for authenticated server access
    --project  <PROJECT_ID>               Optional project_id for project-scoped memory bootstrap
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_SERVER = os.environ.get("SUPERMEMORY_SERVER_URL", "http://127.0.0.1:8000")
DEFAULT_OLLAMA = os.environ.get("SUPERMEMORY_OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL  = "qwen3:1.7b"
STATE_FILE     = Path.home() / ".supermemory_scan_state.json"
LOG_FILE       = Path.home() / ".supermemory_scan.log"
BATCH_SIZE     = 20       # memories per API call
MAX_FILE_BYTES = 512_000  # skip files larger than this
MAX_CHUNK_CHARS = 2000    # max chars per memory chunk
MAX_JSONL_LINES = 200     # max lines to read from conversation history
API_BATCH_RETRY_ATTEMPTS = 3
API_BATCH_RETRY_BASE_DELAY = 1.0
_RETRYABLE_HTTP_CODES = {429, 502, 503, 504}

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "extensions",
              "vendor_imports", "marketplaces", "cache"}
_SKIP_EXT  = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff",
               ".ttf", ".zip", ".tar", ".gz", ".exe", ".dll", ".pyc"}

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("client_scan")

import datetime

def _log(msg: str, verbose: bool = False) -> None:
    """Write progress to log file always; also print to stderr if verbose."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if verbose:
        print(line, file=sys.stderr)


def api_headers(api_key: str = "") -> dict[str, str]:
    key = api_key or os.environ.get("MEMORY_SERVER_API_KEY", "") or os.environ.get("API_KEY", "")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if key:
        headers["X-Api-Key"] = key
    return headers


# ── State (deduplication by file hash) ────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ── Local LLM (Ollama) ─────────────────────────────────────────────────────────

def ollama_available(ollama_url: str) -> bool:
    try:
        req = urllib.request.Request(f"{ollama_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def llm_summarize(text: str, model: str, ollama_url: str, focus: str = "") -> Optional[str]:
    """Use Ollama to summarize text. Returns None on failure."""
    focus_line = f"Focus on: {focus}\n" if focus else ""
    prompt = (
        f"/no_think\n"
        f"Summarize the following content into 2-5 concise bullet points.\n"
        f"Extract: key decisions, facts, preferences, technical details worth remembering.\n"
        f"{focus_line}"
        f"Return plain text bullets, no markdown headers.\n\n"
        f"Content:\n{text[:3000]}\n\nSummary:"
    )
    try:
        body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(
            f"{ollama_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
            text = resp.get("response", "").strip()
            # Strip <think> blocks (qwen3)
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text if text else None
    except Exception as e:
        log.debug("LLM summarize failed: %s", e)
        return None


def llm_extract_facts(conversation_text: str, model: str, ollama_url: str, verbose: bool = False) -> Optional[str]:
    """Extract memorable facts from a conversation using Ollama."""
    _log(f"  LLM extracting facts ({len(conversation_text)} chars)...", verbose)
    prompt = (
        f"/no_think\n"
        f"Extract facts, decisions, preferences and technical details from this "
        f"AI conversation that are worth remembering for future sessions.\n"
        f"Return ONLY a JSON array of strings, each string is one fact.\n"
        f"Maximum 8 facts. Skip greetings and filler.\n\n"
        f"Conversation:\n{conversation_text[:4000]}\n\nJSON array:"
    )
    try:
        body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(
            f"{ollama_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read())
            raw = resp.get("response", "").strip()
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            # Extract JSON array
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                facts = json.loads(match.group())
                return "\n".join(f"- {f}" for f in facts if isinstance(f, str))
    except Exception as e:
        log.debug("LLM extract failed: %s", e)
    return None


# ── File parsers ───────────────────────────────────────────────────────────────

def parse_markdown(path: Path) -> list[dict]:
    text = path.read_text(errors="replace")
    sections = re.split(r"\n(?=#{1,2} )", text.strip())
    chunks = []
    for s in sections:
        s = s.strip()
        if len(s) > 40:
            chunks.append({
                "content": s[:MAX_CHUNK_CHARS],
                "category": "skill" if "SKILL" in path.name.upper() else "context",
                "importance": 0.7 if "CLAUDE" in path.name.upper() else 0.6,
                "tags": [path.stem.lower(), "markdown"],
            })
    return chunks or [{"content": text[:MAX_CHUNK_CHARS], "category": "context",
                       "importance": 0.5, "tags": [path.stem.lower()]}]


def parse_json(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(errors="replace"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    lines = []
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list) and len(v) < 10:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)[:200]}")
    if not lines:
        return []
    return [{"content": f"Settings {path.name}:\n" + "\n".join(lines),
             "category": "setting", "importance": 0.6, "tags": ["settings"]}]


def parse_toml(path: Path) -> list[dict]:
    try:
        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        try:
            # Fallback: simple key=value parser
            text = path.read_text(errors="replace")
            pairs = re.findall(r'^(\w[\w.]*)\s*=\s*"?([^"\n]+)"?', text, re.MULTILINE)
            data = dict(pairs)
        except Exception:
            return []
    lines = [f"{k}: {v}" for k, v in data.items()
             if isinstance(v, (str, int, float, bool))]
    if not lines:
        return []
    return [{"content": f"Config {path.name}:\n" + "\n".join(lines[:30]),
             "category": "config", "importance": 0.6, "tags": ["config"]}]


def parse_python(path: Path) -> list[dict]:
    source = path.read_text(errors="replace")
    lines = []
    in_doc = False
    for line in source.splitlines()[:60]:
        s = line.strip()
        if s.startswith(('"""', "'''")):
            in_doc = not in_doc
            lines.append(line)
        elif in_doc or s.startswith(("def ", "async def ", "#")):
            lines.append(line)
    if not lines:
        return []
    return [{"content": f"Script {path.name}:\n" + "\n".join(lines),
             "category": "context", "importance": 0.5, "tags": ["python", "hook"]}]


def parse_ini(path: Path) -> list[dict]:
    text = path.read_text(errors="replace")
    lines = []
    for line in text.splitlines()[:120]:
        s = line.strip()
        if not s:
            continue
        if s.startswith(("[", "#", ";")):
            lines.append(line)
            continue
        if "=" in s:
            key, value = s.split("=", 1)
            lines.append(f"{key.strip()} = {value.strip()}")
    if not lines:
        return []
    return [{
        "content": f"INI {path.name}:\n" + "\n".join(lines[:80]),
        "category": "config",
        "importance": 0.72,
        "tags": ["ini", "config"],
    }]


def parse_shell(path: Path) -> list[dict]:
    text = path.read_text(errors="replace")
    lines = []
    for line in text.splitlines()[:120]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#!"):
            lines.append(line)
            continue
        if s.startswith("#"):
            lines.append(line)
            continue
        if any(
            s.startswith(prefix)
            for prefix in ("export ", "source ", ".", "cd ", "exec ", "linuxcnc ", "halcmd ")
        ):
            lines.append(line)
            continue
        if "=" in s and " " not in s.split("=", 1)[0]:
            lines.append(line)
    if not lines:
        return []
    return [{
        "content": f"Shell script {path.name}:\n" + "\n".join(lines[:80]),
        "category": "context",
        "importance": 0.62,
        "tags": ["shell", "startup"],
    }]


def parse_hal(path: Path) -> list[dict]:
    text = path.read_text(errors="replace")
    lines = []
    interesting = ("loadrt ", "loadusr ", "addf ", "net ", "setp ", "source ", "call ", "sets ")
    for line in text.splitlines()[:200]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            lines.append(line)
            continue
        if s.startswith(interesting):
            lines.append(line)
    if not lines:
        return []
    return [{
        "content": f"HAL {path.name}:\n" + "\n".join(lines[:120]),
        "category": "config",
        "importance": 0.74,
        "tags": ["hal", "linuxcnc"],
    }]


def parse_jsonl(path: Path, use_llm: bool, model: str, ollama_url: str = DEFAULT_OLLAMA) -> list[dict]:
    """Parse conversation history — use LLM to extract facts if available."""
    raw = path.read_text(errors="replace")
    messages = []
    for line in raw.splitlines()[-MAX_JSONL_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        # Support Claude Code format and plain {role, content}
        if "message" in obj and isinstance(obj["message"], dict):
            role = obj["message"].get("role")
            content = obj["message"].get("content", "")
        else:
            role = obj.get("role")
            content = obj.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if isinstance(content, str) and content.strip() and role:
            messages.append(f"{role.upper()}: {content.strip()[:300]}")

    if not messages:
        return []

    conversation_text = "\n".join(messages)
    session_id = path.stem[:16]

    if use_llm and len(conversation_text) > 500:
        facts = llm_extract_facts(conversation_text, model, ollama_url, verbose=False)
        if facts:
            return [{
                "content": f"[Session {session_id}] Key facts:\n{facts}",
                "category": "conversation",
                "importance": 0.65,
                "tags": ["conversation", "llm-extracted", path.parent.parent.name[:20]],
            }]

    # Fallback: raw digest
    digest = "\n".join(messages[-10:])
    return [{
        "content": f"[Session {session_id}] Recent messages:\n{digest[:MAX_CHUNK_CHARS]}",
        "category": "conversation",
        "importance": 0.45,
        "tags": ["conversation", "raw", path.parent.parent.name[:20]],
    }]


def parse_file(path: Path, use_llm: bool, model: str, ollama_url: str = DEFAULT_OLLAMA) -> list[dict]:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix == ".md":
        return parse_markdown(path)
    if suffix == ".ini":
        return parse_ini(path)
    if suffix == ".sh":
        return parse_shell(path)
    if suffix == ".hal":
        return parse_hal(path)
    if suffix == ".toml":
        return parse_toml(path)
    if suffix == ".py":
        return parse_python(path)
    if suffix == ".jsonl":
        return parse_jsonl(path, use_llm, model, ollama_url)
    if suffix == ".json" or name.endswith(".json"):
        return parse_json(path)
    return []


# ── Directory discovery ────────────────────────────────────────────────────────

def find_ai_dirs() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".claude",
        home / ".codex",
        home / ".continue",
        home / ".cursor",
        home / "AppData" / "Roaming" / "Claude",
        home / "AppData" / "Local" / "Claude",
    ]
    return [p for p in candidates if p.exists() and p.is_dir()]


# ── Server API ────────────────────────────────────────────────────────────────

def _is_retryable_batch_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) in _RETRYABLE_HTTP_CODES
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return "timed out" in text or "temporarily unavailable" in text or "connection reset" in text


def api_batch_store(server: str, memories: list[dict], api_key: str = "") -> dict:
    body = json.dumps({"memories": memories}, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, API_BATCH_RETRY_ATTEMPTS + 1):
        req = urllib.request.Request(
            f"{server}/api/v1/memories/batch",
            data=body,
            headers=api_headers(api_key),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:
            last_error = exc
            if attempt >= API_BATCH_RETRY_ATTEMPTS or not _is_retryable_batch_error(exc):
                break
            delay = API_BATCH_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.warning(
                "memories/batch temporary failure (attempt %d/%d): %s; retry in %.1fs",
                attempt,
                API_BATCH_RETRY_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise RuntimeError("memories/batch request failed with unknown error")


def api_health(server: str, api_key: str = "") -> bool:
    try:
        req = urllib.request.Request(f"{server}/api/v1/health", headers=api_headers(api_key), method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan(
    server: str,
    agent_id: str,
    model: str,
    ollama_url: str,
    api_key: str,
    project: str,
    dirs: list[Path],
    dry_run: bool,
    force: bool,
    no_llm: bool,
    verbose: bool,
) -> dict:
    state = load_state() if not force else {}
    use_llm = ollama_available(ollama_url) and not no_llm and not dry_run

    llm_info = f"on ({ollama_url})" if use_llm else "off"
    _log(f"START server={server} agent={agent_id} llm={llm_info} dirs={len(dirs)}", verbose)

    chunks_to_send: list[dict] = []
    new_state = dict(state)
    stats: dict[str, int] = defaultdict(int)

    for root in dirs:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in _SKIP_EXT:
                continue
            try:
                size = path.stat().st_size
            except Exception:
                continue
            if size > MAX_FILE_BYTES or size < 10:
                continue

            stats["files_seen"] += 1
            fhash = file_hash(path)
            path_key = str(path)

            if not force and state.get(path_key) == fhash:
                stats["skipped_unchanged"] += 1
                continue

            if use_llm and path.suffix.lower() == ".jsonl":
                _log(f"  LLM parse: {path.name}", verbose)

            try:
                chunks = parse_file(path, use_llm, model, ollama_url)
            except Exception as e:
                log.debug("Parse failed %s: %s", path, e)
                _log(f"  ERROR parse {path.name}: {e}", verbose)
                stats["parse_errors"] += 1
                continue

            if not chunks:
                continue

            # Add agent_id and source to each chunk
            for ch in chunks:
                ch["agent_id"] = agent_id
                prefix = "client-scan:"
                path_part = path_key[-(128 - len(prefix)):] if len(path_key) > 128 - len(prefix) else path_key
                ch["source"] = prefix + path_part
                ch.setdefault("memory_type", "context")
                ch.setdefault("importance_score", ch.pop("importance", 0.5))
                ch.setdefault("meta", {})
                ch["meta"]["source_path"] = path_key
                if project:
                    ch["project"] = project
                    tags = list(ch.get("tags") or [])
                    project_tag = f"project:{project}"
                    if project_tag not in tags:
                        tags.append(project_tag)
                    ch["tags"] = tags

            chunks_to_send.extend(chunks)
            new_state[path_key] = fhash
            stats["files_parsed"] += 1
            stats[f"cat_{chunks[0].get('category','?')}"] += len(chunks)

            if verbose:
                llm_tag = " [llm]" if use_llm and path.suffix == ".jsonl" else ""
                _log(f"  [{chunks[0].get('category','?')}]{llm_tag} {path.name} -> {len(chunks)} chunk(s)", verbose)

    stats["chunks_total"] = len(chunks_to_send)
    _log(f"  parsed {stats['files_parsed']} files, {len(chunks_to_send)} chunks, skipped {stats.get('skipped_unchanged', 0)} unchanged", verbose)

    if dry_run:
        _log(f"DRY-RUN done — would send {len(chunks_to_send)} chunks", verbose)
        print(f"\n[dry-run] Would send {len(chunks_to_send)} chunks from {stats['files_parsed']} files")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return dict(stats)

    # Send in batches
    stored = 0
    failed = 0
    total_batches = (len(chunks_to_send) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(chunks_to_send), BATCH_SIZE):
        batch = chunks_to_send[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        _log(f"  sending batch {batch_num}/{total_batches} ({len(batch)} chunks)...", verbose)
        try:
            result = api_batch_store(server, batch, api_key=api_key)
            stored += len(result.get("created_ids", []))
            failed += result.get("failed_count", 0)
        except Exception as e:
            log.warning("Batch %d failed: %s", batch_num, e)
            _log(f"  ERROR batch {batch_num}: {e}", verbose)
            failed += len(batch)

    stats["stored"] = stored
    stats["failed"] = failed
    _log(f"DONE stored={stored} failed={failed}", verbose)

    if stored > 0 or not chunks_to_send:
        save_state(new_state)

    return dict(stats)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SuperMemory client scanner")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA, help="Ollama URL (local or network)")
    parser.add_argument("--api-key", default="", help="API key for authenticated SuperMemory server access")
    parser.add_argument("--project", default="", help="Optional project_id to attach to stored memories")
    parser.add_argument("--agent", default=socket.gethostname().lower())
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dirs", nargs="*", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-llm", action="store_true", help="Skip local LLM, use fast rule-based parsing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    _log(f"=== client_scan start (pid={os.getpid()}) ===", args.verbose)

    # Check server
    if not api_health(args.server, api_key=args.api_key):
        _log(f"ERROR server not reachable: {args.server}", True)
        sys.exit(1)

    # Resolve directories
    if args.dirs:
        dirs = [Path(d) for d in args.dirs]
    else:
        dirs = find_ai_dirs()
        if not dirs:
            print("[ERROR] No AI directories found. Use --dirs to specify manually.", file=sys.stderr)
            sys.exit(1)

    stats = scan(
        server=args.server,
        agent_id=args.agent,
        model=args.model,
        ollama_url=args.ollama,
        api_key=args.api_key,
        project=args.project,
        dirs=dirs,
        dry_run=args.dry_run,
        force=args.force,
        no_llm=args.no_llm,
        verbose=args.verbose,
    )

    # Summary
    if not args.dry_run:
        llm_status = f"on ({args.ollama})" if ollama_available(args.ollama) else "off"
        print(
            f"[supermemory] scan done — "
            f"files={stats.get('files_parsed', 0)}  "
            f"chunks={stats.get('chunks_total', 0)}  "
            f"stored={stats.get('stored', 0)}  "
            f"skipped={stats.get('skipped_unchanged', 0)}  "
            f"llm={llm_status}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
