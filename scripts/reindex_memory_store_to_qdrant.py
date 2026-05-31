"""Copy memory_store SQLite rows back into the Qdrant agent_memories collection."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import settings
from app.models.enums import MemoryType
from app.models.memory import MemoryCreate
from app.services.ollama_service import OllamaService
from app.services.qdrant_service import QdrantService
from app.services.system_data_root import data_path
from qdrant_client import AsyncQdrantClient

logger = logging.getLogger("reindex_memory_store")

RESERVED_META_KEYS = {
    "agent_id",
    "memory_type",
    "importance_score",
    "source",
    "tags",
    "session_id",
    "status",
    "decay_rate",
    "pinned",
    "project",
    "topic_path",
    "scope",
    "related_ids",
    "supports",
    "canonical_id",
    "expires_at",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild Qdrant agent_memories from the system data root memory_store.db.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=data_path("memory_store.db", create_parent=False),
        help="SQLite file holding the memory_content rows.",
    )
    parser.add_argument(
        "--category",
        "-c",
        action="append",
        help="Scope the import to specific categories (repeatable). Defaults to all.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit how many rows to re-import (applies after filters).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only preview the rows that would be processed; do not embed or call Qdrant.",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=5,
        help="When running with --dry-run, show this many rows.",
    )
    parser.add_argument(
        "--reset-collection",
        action="store_true",
        help="Delete and recreate the target Qdrant collection before importing.",
    )
    parser.add_argument(
        "--ollama-url",
        default=settings.ollama_base_url,
        help="Base URL for the Ollama embedding service.",
    )
    parser.add_argument(
        "--ollama-model",
        default=settings.ollama_embedding_model,
        help="Ollama embedding model name.",
    )
    parser.add_argument(
        "--qdrant-host",
        default=settings.qdrant_host,
        help="Hostname for the Qdrant endpoint.",
    )
    parser.add_argument(
        "--qdrant-port",
        type=int,
        default=settings.qdrant_port,
        help="Port for the Qdrant endpoint.",
    )
    parser.add_argument(
        "--collection",
        default=settings.qdrant_collection_name,
        help="Target Qdrant collection name.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level for the rebuild run.",
    )
    return parser.parse_args()


def _build_query(category_filters: list[str] | None, limit: int | None) -> tuple[str, str, list[Any]]:
    where_clause = ""
    params: list[Any] = []
    if category_filters:
        placeholders = ",".join("?" for _ in category_filters)
        where_clause = f" WHERE category IN ({placeholders})"
        params.extend(category_filters)
    count_query = f"SELECT COUNT(*) FROM memory_content{where_clause}"
    select_query = (
        f"SELECT memory_id, category, content, metadata FROM memory_content"
        f"{where_clause} ORDER BY updated_at DESC"
    )
    if limit and limit > 0:
        select_query = f"{select_query} LIMIT {limit}"
    return count_query, select_query, params


def _load_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning("Failed to parse metadata JSON (%s)", raw)
        return {}


def _normalize_list(value: Any | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, (list, tuple)):
                return [str(item) for item in decoded if item is not None]
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value)]


def _to_bool(value: Any | None) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _to_float(value: Any | None, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        number = float(value)
        return max(min(number, maximum), minimum)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            try:
                return datetime.fromtimestamp(float(value))
            except (TypeError, ValueError):
                return None
    return None


def _memory_type_from_metadata(value: Any | None) -> MemoryType:
    if isinstance(value, str) and value:
        try:
            return MemoryType(value)
        except ValueError:
            return MemoryType.fact
    return MemoryType.fact


def _build_memory(row: sqlite3.Row) -> MemoryCreate:
    metadata = _load_metadata(row["metadata"])
    agent_id = metadata.get("agent_id") or "memory_store-import"
    memory_type = _memory_type_from_metadata(metadata.get("memory_type"))
    importance_score = _to_float(metadata.get("importance_score"), 0.5)
    tags = _normalize_list(metadata.get("tags"))
    if not tags and metadata.get("tag"):
        tags = _normalize_list(metadata.get("tag"))
    session_id = metadata.get("session_id")
    status = metadata.get("status")
    decay_rate = metadata.get("decay_rate")
    pinned = _to_bool(metadata.get("pinned"))
    project = metadata.get("project")
    topic_path = metadata.get("topic_path")
    scope = metadata.get("scope") or "project"
    supports = _normalize_list(metadata.get("supports"))
    canonical_id = metadata.get("canonical_id")
    related_ids = _normalize_list(metadata.get("related_ids"))
    expires_at = _parse_datetime(metadata.get("expires_at"))
    source = metadata.get("source") or "memory_store_import"
    meta = {
        key: value
        for key, value in metadata.items()
        if key not in RESERVED_META_KEYS
    }
    content = str(row["content"] or "").strip()
    if not content:
        raise ValueError("empty content")
    return MemoryCreate(
        content=content,
        agent_id=agent_id,
        memory_type=memory_type,
        category=str(row["category"] or "general"),
        importance_score=importance_score,
        source=source,
        tags=tags,
        session_id=session_id,
        status=status,
        meta=meta,
        decay_rate=float(decay_rate) if decay_rate is not None else None,
        pinned=pinned,
        related_ids=related_ids,
        project=project,
        expires_at=expires_at,
        topic_path=topic_path,
        scope=scope,
        supports=supports,
        canonical_id=canonical_id,
    )


async def main() -> None:
    args = parse_args()
    level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", force=True)
    logger.setLevel(level)

    db_path = args.db_path
    if not db_path.exists():
        logger.error("SQLite file %s not found", db_path)
        return
    count_query, select_query, base_params = _build_query(args.category, args.limit)
    count_params = list(base_params)
    select_params = list(base_params)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(count_query, count_params).fetchone()[0]
    except sqlite3.Error as exc:
        logger.error("Failed to count rows: %s", exc)
        conn.close()
        return

    if total == 0:
        logger.info("No memory_content rows matched filters; nothing to import.")
        conn.close()
        return

    logger.info("Preparing to import %d rows from %s", total, db_path)

    if args.dry_run:
        cursor = conn.execute(select_query, select_params)
        logger.info("Dry run: previewing up to %d rows", args.preview)
        for idx, row in enumerate(cursor, start=1):
            if idx > args.preview:
                break
            logger.info("[%d/%d] id=%s category=%s", idx, total, row["memory_id"], row["category"])
        cursor.close()
        conn.close()
        return

    client = AsyncQdrantClient(host=args.qdrant_host, port=args.qdrant_port)
    settings.qdrant_collection_name = args.collection
    qdrant = QdrantService(client)
    ollama = OllamaService(base_url=args.ollama_url, model=args.ollama_model)
    start = time.perf_counter()
    imported = 0
    try:
        if args.reset_collection:
            try:
                await client.delete_collection(collection_name=args.collection)
                logger.info("Dropped existing collection %s", args.collection)
            except Exception as exc:
                logger.warning("Failed to drop collection (it may not exist yet): %s", exc)
        await qdrant.ensure_collection()
        cursor = conn.execute(select_query, select_params)
        for idx, row in enumerate(cursor, start=1):
            try:
                memory = _build_memory(row)
            except ValueError as exc:
                logger.warning("Skipping row %s (%s): %s", row["memory_id"], row["category"], exc)
                continue
            try:
                vector = await ollama.embed(memory.content)
            except Exception as exc:
                logger.error("Embedding error for %s: %s", row["memory_id"], exc)
                continue
            try:
                await qdrant.insert(memory, vector)
                imported += 1
            except Exception as exc:
                logger.error("Qdrant insert failed for %s: %s", row["memory_id"], exc)
            if idx % 10 == 0:
                elapsed = time.perf_counter() - start
                logger.info("Processed %d/%d rows (imported %d) in %.1fs", idx, total, imported, elapsed)
        cursor.close()
    finally:
        await ollama.close()
        await client.close()
        conn.close()
    elapsed = time.perf_counter() - start
    logger.info("Import complete: %d/%d rows in %.1fs", imported, total, elapsed)


if __name__ == "__main__":
    asyncio.run(main())
