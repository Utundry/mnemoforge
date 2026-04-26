from __future__ import annotations

import argparse
import asyncio
import logging

from qdrant_client import AsyncQdrantClient

from app.config import settings
from app.services.ollama_service import OllamaService
from app.services.project_tasks_rebuilder import rebuild_project_tasks


async def _run(project: str | None, limit: int, changes_limit: int) -> None:
    if settings.qdrant_in_memory:
        client = AsyncQdrantClient(":memory:")
    else:
        client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    ollama = OllamaService()
    try:
        summary = await rebuild_project_tasks(
            qdrant_client=client,
            ollama=ollama,
            project=project,
            limit=limit,
            changes_limit=changes_limit,
        )
        logging.info(
            "Rebuilt task memories for project=%s (tasks=%d changes=%d)",
            summary["project"],
            summary["tasks"],
            summary["changes"],
        )
    finally:
        await ollama.close()
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild project task and task_change memories from the durable SQLite store."
    )
    parser.add_argument("--project", help="Project filter for tasks")
    parser.add_argument("--limit", type=int, default=0, help="Max tasks to rebuild (0 = all available up to 2000)")
    parser.add_argument(
        "--changes-limit",
        type=int,
        default=0,
        help="Max change entries per task (0 = 100)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(args.project, args.limit, args.changes_limit))


if __name__ == "__main__":
    main()
