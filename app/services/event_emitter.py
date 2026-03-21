"""
event_emitter.py — thin fire-and-forget wrapper around learning_store.write_event().

Usage in route handlers (via BackgroundTasks):
    background_tasks.add_task(emit, "memory_write", agent_id=body.agent_id, ...)

Never raises — failures are logged at DEBUG and silently swallowed so they
never affect the main request.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def emit(
    event_type: str,
    *,
    agent_id: str = "",
    project: str = "",
    transport: str = "api",
    episode_id: str = "",
    context_signature: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    """Record a canonical learning event. Non-fatal — never blocks the caller."""
    try:
        from app.services.learning_store import get_learning_store
        await get_learning_store().write_event(
            event_type=event_type,
            agent_id=agent_id,
            project=project,
            transport=transport,
            episode_id=episode_id,
            context_signature=context_signature,
            payload=payload or {},
        )
    except Exception as exc:
        logger.debug("event_emitter: failed to record %s — %s", event_type, exc)
