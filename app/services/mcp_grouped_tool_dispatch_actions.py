from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.mcp_memory_actions import MemoryActionDependencies, execute_memory_action
from app.services.mcp_runtime_utility_actions import (
    RuntimeUtilityActionDependencies,
    execute_runtime_utility_action,
)


GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
DeleteCallback = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any] | None]]


MEMORY_ACTIONS = {
    "memory_store",
    "memory_search",
    "memory_tree_slice",
    "memory_context",
    "record_memory_outcome",
    "memory_recent",
    "memory_get",
    "memory_delete",
    "memory_batch_store",
    "memory_cleanup",
}

RUNTIME_UTILITY_ACTIONS = {
    "system_info",
    "memory_stats",
    "registry_best",
    "registry_update",
    "registry_components",
    "model_available",
    "report_limit_hit",
}


@dataclass(frozen=True)
class GroupedToolDispatchDependencies:
    get: GetCallback
    post: PostCallback
    delete: DeleteCallback


async def execute_grouped_memory_or_runtime_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: GroupedToolDispatchDependencies,
) -> str | None:
    if name in MEMORY_ACTIONS:
        return await execute_memory_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=MemoryActionDependencies(
                get=dependencies.get,
                post=dependencies.post,
                delete=dependencies.delete,
            ),
        )
    if name in RUNTIME_UTILITY_ACTIONS:
        return await execute_runtime_utility_action(
            name=name,
            args=args,
            api_base=api_base,
            dependencies=RuntimeUtilityActionDependencies(
                get=dependencies.get,
                post=dependencies.post,
            ),
        )
    return None
