"""
Background file watcher service.

Uses watchdog to monitor AI assistant directories for changes.
On file create/modify → parses and ingests into Qdrant automatically.
Singleton — managed by FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from threading import Thread
from typing import Optional

try:
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    from watchdog.observers import Observer
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment,misc]

from app.services.ai_dir_parser import (
    ParsedChunk,
    ParsedConversation,
    _SKIP_DIRS,
    _SKIP_EXTENSIONS,
    MAX_FILE_BYTES,
    extract_jsonl_conversation,
    parse_markdown,
    parse_settings_json,
    parse_toml,
    parse_python_hook,
    parse_jsonl_conversation,
)

logger = logging.getLogger(__name__)

if not _WATCHDOG_AVAILABLE:
    logger.warning("[watcher] watchdog not installed — file watching disabled. Run: pip install watchdog")

# Injected at startup
_qdrant_svc = None
_ollama_svc = None
_processed_conversation_hashes: dict[str, str] = {}


def set_services(qdrant, ollama) -> None:
    global _qdrant_svc, _ollama_svc
    _qdrant_svc = qdrant
    _ollama_svc = ollama


# ── Event handler ─────────────────────────────────────────────────────────────

class _AIDirectoryHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, agent_id: str):
        self._loop = loop
        self._agent_id = agent_id

    def _should_process(self, path_str: str) -> bool:
        path = Path(path_str)
        if any(part in _SKIP_DIRS for part in path.parts):
            return False
        if path.suffix.lower() in _SKIP_EXTENSIONS:
            return False
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return False
        except Exception:
            return False
        return True

    def _parse(self, path: Path) -> list[ParsedChunk]:
        name = path.name.lower()
        suffix = path.suffix.lower()
        if suffix == ".md":
            return parse_markdown(path)
        if name == "settings.json" or (suffix == ".json" and "setting" in name):
            return parse_settings_json(path)
        if suffix == ".toml":
            return parse_toml(path)
        if suffix == ".py":
            return parse_python_hook(path)
        if suffix == ".jsonl":
            return parse_jsonl_conversation(path)
        return []

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            asyncio.run_coroutine_threadsafe(
                self._ingest(Path(event.src_path), "created"),
                self._loop,
            )

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            asyncio.run_coroutine_threadsafe(
                self._ingest(Path(event.src_path), "modified"),
                self._loop,
            )

    async def _ingest(self, path: Path, event_type: str) -> None:
        if _qdrant_svc is None or _ollama_svc is None:
            return
        try:
            chunks = self._parse(path)
            for chunk in chunks:
                await _store_chunk(chunk, self._agent_id)
            if path.suffix.lower() == ".jsonl":
                conversation = extract_jsonl_conversation(path)
                if conversation is not None:
                    await _analyze_conversation(conversation, self._agent_id)
            if chunks:
                logger.info("[watcher] %s %s -> ingested %d chunks", event_type, path.name, len(chunks))
        except Exception as e:
            logger.warning("[watcher] failed to ingest %s: %s", path, e)


# ── Chunk storage ──────────────────────────────────────────────────────────────

async def _store_chunk(chunk: ParsedChunk, agent_id: str) -> None:
    from app.models.memory import MemoryCreate
    from app.models.enums import MemoryType

    mem = MemoryCreate(
        content=chunk.content,
        agent_id=agent_id,
        memory_type=MemoryType.context,
        category=chunk.category,
        importance_score=chunk.importance,
        source=f"watcher:{chunk.source_path}",
        tags=chunk.tags + [chunk.file_hash] if chunk.file_hash else chunk.tags,
    )
    vector = await _ollama_svc.embed(mem.content)
    await _qdrant_svc.insert(mem, vector)


async def _analyze_conversation(conversation: ParsedConversation, agent_id: str, transport: str = "watcher") -> None:
    from app.config import settings

    if not settings.watcher_enable_dialogue_analysis:
        return
    if not conversation.file_hash or len(conversation.transcript.strip()) < 20:
        return

    last_hash = _processed_conversation_hashes.get(conversation.source_path)
    if last_hash == conversation.file_hash:
        return

    from app.routers.skills import analyze_dialogue_transcript

    await analyze_dialogue_transcript(
        transcript=conversation.transcript,
        agent_id=agent_id,
        qdrant=_qdrant_svc,
        ollama=_ollama_svc,
        session_id=conversation.session_id or None,
        source_path=f"watcher:{conversation.source_path}",
        file_hash=conversation.file_hash,
        transport=transport,
    )
    _processed_conversation_hashes[conversation.source_path] = conversation.file_hash


# ── Watcher singleton ──────────────────────────────────────────────────────────

class WatcherService:
    def __init__(self):
        self._observer: Optional[Observer] = None
        self._watched: list[str] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[Thread] = None
        self.running: bool = False

    def start(self, dirs: list[Path], agent_id: str = "ai-dirs") -> list[str]:
        if not _WATCHDOG_AVAILABLE:
            logger.warning("[watcher] cannot start — watchdog not installed")
            return []
        if self.running:
            return self._watched

        self._loop = asyncio.get_running_loop()
        self._observer = Observer()
        handler = _AIDirectoryHandler(self._loop, agent_id)
        watched = []
        for d in dirs:
            if d.exists():
                self._observer.schedule(handler, str(d), recursive=True)
                watched.append(str(d))
                logger.info("[watcher] watching %s", d)

        self._observer.start()
        self._watched = watched
        self.running = True
        return watched

    def stop(self) -> None:
        if self._observer and self.running:
            self._observer.stop()
            self._observer.join(timeout=5)
            self.running = False
            self._watched = []
            logger.info("[watcher] stopped")

    def status(self) -> dict:
        return {
            "running": self.running,
            "watched_dirs": self._watched,
            "dir_count": len(self._watched),
        }


# Global singleton
watcher = WatcherService()
