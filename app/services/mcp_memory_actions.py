from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
DeleteCallback = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class MemoryActionDependencies:
    get: GetCallback
    post: PostCallback
    delete: DeleteCallback


async def execute_memory_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: MemoryActionDependencies,
) -> str:
    if name == "memory_store":
        data = await dependencies.post(api_base, "/memories", args)
        return f"Stored memory {data['id']}\n{json.dumps(data, indent=2, ensure_ascii=False)}"

    if name == "memory_search":
        results = await dependencies.post(api_base, "/memories/search", args)
        if not results:
            return "No memories found."
        lines = []
        for result in results:
            memory = result["memory"]
            lines.append(
                f"[{result['score']:.3f}] ({memory['memory_type']}) {memory['content'][:200]}\n"
                f"  id={memory['id']}"
            )
        return "\n\n".join(lines)

    if name == "memory_tree_slice":
        data = await dependencies.post(api_base, "/knowledge-tree/slice", args)
        lines = [f"Target Category: {data.get('target_category', 'general')}\n"]
        for result in data.get("results", []):
            memory = result.get("memory", {})
            lines.append(
                f"[{result.get('score', 0):.3f} | boost:+{result.get('tree_boost', 0):.3f}] "
                f"({memory.get('category', 'general')}) {memory.get('content', '')[:200]}\n"
                f"  id={memory.get('id')} path={memory.get('topic_path')}"
            )
        return "\n\n".join(lines)

    if name == "memory_context":
        data = await dependencies.post(api_base, "/memories/context", args)
        session_id = data.get("session_id") or "-"
        context = data.get("context") or ""
        snippet = context[:800]
        more = "..." if len(context) > len(snippet) else ""
        return (
            f"session_id={session_id} used={data.get('used_count',0)} sources={data.get('source_count',0)} "
            f"scope_expanded={bool(data.get('scope_expanded'))}\n\n"
            f"{snippet}{more}"
        )

    if name == "record_memory_outcome":
        data = await dependencies.post(api_base, "/outcomes", args)
        return (
            f"Recorded outcome: success={data.get('success')} session_id={data.get('session_id') or args.get('session_id')}\n"
            f"updated={data.get('updated',0)} skipped={data.get('skipped',0)}"
        )

    if name == "memory_recent":
        params = f"?minutes={args.get('minutes', 10)}&limit={args.get('limit', 20)}"
        if args.get("agent_id"):
            params += f"&agent_id={args['agent_id']}"
        results = await dependencies.get(api_base, f"/memories/recent{params}")
        if not results:
            return "No recent memories found."
        lines = []
        for memory in results:
            lines.append(
                f"[{memory['timestamp'][:19]}] ({memory['agent_id']}) {memory['content'][:200]}\n"
                f"  id={memory['id']}"
            )
        return "\n\n".join(lines)

    if name == "memory_get":
        data = await dependencies.get(api_base, f"/memories/{args['memory_id']}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "memory_delete":
        await dependencies.delete(api_base, f"/memories/{args['memory_id']}", None)
        return f"Deleted memory {args['memory_id']}"

    if name == "memory_batch_store":
        memories = args.get("memories", [])
        if isinstance(memories, str):
            memories = json.loads(memories)
        data = await dependencies.post(api_base, "/memories/batch", {"memories": memories})
        return f"Created {len(data['created_ids'])} memories. Failed: {data['failed_count']}"

    if name == "memory_cleanup":
        data = await dependencies.delete(api_base, "/memories/cleanup", args)
        deleted_count = (data or {}).get("deleted_count", 0)
        return f"Deleted {deleted_count} memories."

    raise ValueError(f"Unsupported memory action: {name}")
