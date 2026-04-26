"""
Structured parsers for AI assistant directories.

Understands the internal formats of:
  .claude/    — Claude Code (settings, skills, hooks, conversation history)
  .codex/     — OpenAI Codex CLI (config, sessions)
  .continue/  — Continue.dev (config, history)
  CLAUDE.md   — Project context files
  *.jsonl     — Conversation history (extracts key facts via qwen3)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from app.services.text_localization import (
    is_low_quality_text,
    looks_like_mojibake,
    normalize_text_for_display,
)

logger = logging.getLogger(__name__)

# Files larger than this are chunked / summarized, not ingested raw
MAX_FILE_BYTES = 512 * 1024       # 512 KB
MAX_JSONL_MESSAGES = 50           # max messages to extract from a conversation
SUMMARY_MODEL = "qwen3:1.7b"


@dataclass
class ParsedChunk:
    """A unit of content ready to be stored in Qdrant."""
    content: str
    source_path: str
    category: str                  # "skill" | "setting" | "conversation" | "context" | "config"
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    file_hash: str = ""            # SHA256 of source file — used for dedup


@dataclass
class ParsedConversation:
    """Structured conversation excerpt ready for downstream dialogue analysis."""
    transcript: str
    source_path: str
    file_hash: str
    session_id: str = ""
    user_messages: int = 0
    assistant_messages: int = 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _extract_message_text(raw_content) -> str:
    if isinstance(raw_content, list):
        parts = []
        for block in raw_content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        raw_content = " ".join(parts)
    if isinstance(raw_content, str):
        normalized = normalize_text_for_display(raw_content)
        if is_low_quality_text(normalized):
            return ""
        return normalized
    return ""


def _load_jsonl_messages(path: Path) -> tuple[str, list[tuple[str, str]]]:
    try:
        raw = path.read_text(errors="replace")
    except Exception:
        return "", []

    if path.stat().st_size < 200:
        return "", []

    file_hash = _sha256(path)
    messages: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        role = None
        raw_content = None
        if "message" in obj and isinstance(obj["message"], dict):
            role = obj["message"].get("role")
            raw_content = obj["message"].get("content", "")
        elif "role" in obj:
            role = obj.get("role")
            raw_content = obj.get("content", "")

        if not isinstance(role, str):
            continue

        content = _extract_message_text(raw_content)
        if content and not looks_like_mojibake(content):
            messages.append((role, content[:1000]))

    return file_hash, messages


# ── Markdown parser ────────────────────────────────────────────────────────────

def parse_markdown(path: Path) -> list[ParsedChunk]:
    """Parse CLAUDE.md, SKILL.md, README.md — split by section."""
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return []

    file_hash = _sha256(path)
    category = "skill" if "SKILL" in path.name.upper() else "context"
    importance = 0.7 if "CLAUDE" in path.name.upper() else 0.6
    tags = [path.stem.lower(), "markdown"]

    # Split on H1/H2 headers
    sections = re.split(r"\n(?=#{1,2} )", text.strip())
    chunks = []
    for section in sections:
        section = section.strip()
        if len(section) < 30:
            continue
        chunks.append(ParsedChunk(
            content=section[:2000],
            source_path=str(path),
            category=category,
            tags=tags,
            importance=importance,
            file_hash=file_hash,
        ))
    if not chunks:
        chunks.append(ParsedChunk(
            content=text[:2000],
            source_path=str(path),
            category=category,
            tags=tags,
            importance=importance,
            file_hash=file_hash,
        ))
    return chunks


# ── JSON / TOML settings parsers ───────────────────────────────────────────────

def parse_settings_json(path: Path) -> list[ParsedChunk]:
    """Parse Claude Code / Codex settings.json — extract meaningful keys."""
    try:
        data = json.loads(path.read_text(errors="replace"))
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    file_hash = _sha256(path)
    lines = []
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list) and len(v) < 20:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")

    if not lines:
        return []

    content = f"Settings from {path.name}:\n" + "\n".join(lines)
    return [ParsedChunk(
        content=content[:1500],
        source_path=str(path),
        category="setting",
        tags=["settings", path.parent.name],
        importance=0.6,
        file_hash=file_hash,
    )]


def parse_toml(path: Path) -> list[ParsedChunk]:
    """Parse config.toml files (Codex, etc.)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []

    file_hash = _sha256(path)
    lines = []

    def flatten(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict):
                flatten(v, f"{key}.")
            elif isinstance(v, (str, int, float, bool)):
                lines.append(f"{key}: {v}")

    flatten(data)
    if not lines:
        return []

    content = f"Config from {path.name}:\n" + "\n".join(lines)
    return [ParsedChunk(
        content=content[:1500],
        source_path=str(path),
        category="config",
        tags=["config", path.stem],
        importance=0.6,
        file_hash=file_hash,
    )]


# ── Python hook parser ────────────────────────────────────────────────────────

def parse_python_hook(path: Path) -> list[ParsedChunk]:
    """Extract docstrings and function signatures from hook scripts."""
    try:
        source = path.read_text(errors="replace")
    except Exception:
        return []

    file_hash = _sha256(path)
    # Extract module docstring + function defs
    lines = []
    in_docstring = False
    for line in source.splitlines()[:80]:
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_docstring = not in_docstring
            lines.append(line)
        elif in_docstring:
            lines.append(line)
        elif stripped.startswith("def ") or stripped.startswith("async def "):
            lines.append(line)
        elif stripped.startswith("#"):
            lines.append(line)

    if not lines:
        return []

    content = f"Hook script {path.name}:\n" + "\n".join(lines)
    return [ParsedChunk(
        content=content[:1000],
        source_path=str(path),
        category="context",
        tags=["hook", "python"],
        importance=0.5,
        file_hash=file_hash,
    )]


# ── JSONL conversation parser ─────────────────────────────────────────────────

def parse_jsonl_conversation(path: Path) -> list[ParsedChunk]:
    """
    Extract key messages from Claude Code / Codex conversation history.
    Takes assistant messages only (they contain the substance),
    limited to last MAX_JSONL_MESSAGES messages.
    """
    file_hash, messages = _load_jsonl_messages(path)

    if not messages:
        return []

    # Take last N messages, prefer assistant messages for substance
    recent = messages[-MAX_JSONL_MESSAGES:]
    assistant_msgs = [(r, c) for r, c in recent if r == "assistant"]
    user_msgs = [(r, c) for r, c in recent if r == "user"]

    chunks = []
    # Digest of assistant responses
    if assistant_msgs:
        digest = "\n---\n".join(c for _, c in assistant_msgs[-10:])
        chunks.append(ParsedChunk(
            content=f"[Conversation {path.stem[:12]}] Assistant responses:\n{digest[:2000]}",
            source_path=str(path),
            category="conversation",
            tags=["conversation", "assistant", path.parent.parent.name[:20]],
            importance=0.5,
            file_hash=file_hash,
        ))
    # User questions (good for search)
    if user_msgs:
        digest = "\n".join(c for _, c in user_msgs[-5:])
        chunks.append(ParsedChunk(
            content=f"[Conversation {path.stem[:12]}] User queries:\n{digest[:1000]}",
            source_path=str(path),
            category="conversation",
            tags=["conversation", "user", path.parent.parent.name[:20]],
            importance=0.4,
            file_hash=file_hash,
        ))
    return chunks


def extract_jsonl_conversation(path: Path) -> Optional[ParsedConversation]:
    """
    Return a role-labelled transcript for downstream dialogue analysis.
    Keeps the recent tail of the conversation where actionable friction usually appears.
    """
    file_hash, messages = _load_jsonl_messages(path)
    if not messages:
        return None

    recent = messages[-MAX_JSONL_MESSAGES:]
    transcript_lines: list[str] = []
    user_count = 0
    assistant_count = 0

    for role, content in recent:
        normalized = role.lower()
        if normalized == "user":
            label = "USER"
            user_count += 1
        elif normalized == "assistant":
            label = "ASSISTANT"
            assistant_count += 1
        else:
            label = normalized.upper()[:24]
        transcript_lines.append(f"{label}: {content}")

    transcript = "\n".join(transcript_lines)[-8000:]
    if len(transcript) < 20:
        return None

    return ParsedConversation(
        transcript=transcript,
        source_path=str(path),
        file_hash=file_hash,
        session_id=path.stem[:120],
        user_messages=user_count,
        assistant_messages=assistant_count,
    )


# ── Directory scanners ────────────────────────────────────────────────────────

# Skip these regardless
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", "extensions",
              "vendor_imports", "marketplaces", "cache"}
_SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff",
                    ".ttf", ".zip", ".tar", ".gz", ".exe", ".dll", ".pyc"}


def scan_directory(root: Path, max_files: int = 500) -> list[ParsedChunk]:
    """
    Recursively scan a directory and parse all supported files.
    Returns list of ParsedChunks ready for ingestion.
    """
    chunks: list[ParsedChunk] = []
    count = 0

    for path in root.rglob("*"):
        if count >= max_files:
            logger.warning("scan_directory: reached max_files=%d limit", max_files)
            break

        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _SKIP_EXTENSIONS:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            logger.debug("Skipping large file: %s", path)
            continue

        count += 1
        name = path.name.lower()
        suffix = path.suffix.lower()

        if suffix == ".md":
            chunks.extend(parse_markdown(path))
        elif name in ("settings.json",) or (suffix == ".json" and "setting" in name):
            chunks.extend(parse_settings_json(path))
        elif suffix == ".toml":
            chunks.extend(parse_toml(path))
        elif suffix == ".py":
            chunks.extend(parse_python_hook(path))
        elif suffix == ".jsonl":
            chunks.extend(parse_jsonl_conversation(path))
        elif suffix == ".json" and path.stat().st_size < 32 * 1024:
            chunks.extend(parse_settings_json(path))

    return chunks


# ── Default AI directories ────────────────────────────────────────────────────

def default_ai_dirs() -> list[Path]:
    """Return list of known AI assistant directories that exist on this system."""
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
