from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.mcp_tool_contracts import (
    build_coordination_status_payload,
    build_list_coordination_query,
    build_pickup_coordination_payload,
    build_send_coordination_message_payload,
    format_coordination_list,
    format_coordination_message,
)


GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


COORDINATION_ACTIONS = {
    "send_coordination_message",
    "pickup_coordination_messages",
    "list_coordination_messages",
    "update_coordination_message_status",
}


@dataclass(frozen=True)
class CoordinationActionDependencies:
    get: GetCallback
    post: PostCallback


async def execute_coordination_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: CoordinationActionDependencies,
) -> str:
    if name == "send_coordination_message":
        data = await dependencies.post(api_base, "/models/coordination/messages", build_send_coordination_message_payload(args))
        return format_coordination_message(data, prefix="Sent coordination message")

    if name == "pickup_coordination_messages":
        data = await dependencies.post(api_base, "/models/coordination/pickup", build_pickup_coordination_payload(args))
        return format_coordination_list(data, empty_text=f"No new coordination messages for agent '{args['agent_id']}'.")

    if name == "list_coordination_messages":
        data = await dependencies.get(api_base, f"/models/coordination/messages?{build_list_coordination_query(args)}")
        return format_coordination_list(data, empty_text="No coordination messages matched the query.")

    if name == "update_coordination_message_status":
        data = await dependencies.post(
            api_base,
            f"/models/coordination/messages/{args['message_id']}/status",
            build_coordination_status_payload(args),
        )
        return format_coordination_message(data, prefix="Updated coordination message")

    raise ValueError(f"Unsupported coordination action: {name}")
