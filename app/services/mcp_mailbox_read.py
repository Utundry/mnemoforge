from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Awaitable, Callable, Any
from urllib.parse import quote

from app.services.mcp_mailbox import build_mailbox_get_packet, build_mailbox_state_packet


SessionIdentityCallback = Callable[[str | None], Awaitable[dict[str, str]]]
GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
_HEALTH_NUDGE_STATELESS_COOLDOWN_SECONDS = 300.0
_STATELESS_HEALTH_NUDGE_SEEN: dict[str, float] = {}


@dataclass(frozen=True)
class MailboxReadDependencies:
    get_session_identity_defaults: SessionIdentityCallback
    get: GetCallback | None = None


async def build_mailbox_state_response(
    *,
    args: dict[str, Any],
    dependencies: MailboxReadDependencies,
    session_id: str | None = None,
    api_base: str = "",
) -> dict[str, Any]:
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    project = str(
        args.get("project")
        or args.get("project_id")
        or identity_defaults.get("project")
        or identity_defaults.get("project_id")
        or "mnemoforge"
    )
    governed_laws = await _read_governed_laws(
        api_base=api_base,
        project=project,
        dependencies=dependencies,
    )
    packet = build_mailbox_state_packet(
        state=str(args.get("state") or "planning"),
        project=project,
        runtime_profile_id=_runtime_profile_id(args, identity_defaults),
        diagnostic=bool(args.get("diagnostic", False)),
        detail=str(args.get("detail") or "compact"),
        governed_laws=governed_laws,
    )
    return await _suppress_repeated_health_nudge(
        packet,
        session_id=session_id,
        scope_key=_health_nudge_scope_key(args),
        diagnostic=bool(args.get("diagnostic", False)),
    )


async def build_mailbox_get_response(
    *,
    args: dict[str, Any],
    dependencies: MailboxReadDependencies,
    session_id: str | None = None,
) -> dict[str, Any]:
    identity_defaults = await dependencies.get_session_identity_defaults(session_id)
    project = str(
        args.get("project")
        or args.get("project_id")
        or identity_defaults.get("project")
        or identity_defaults.get("project_id")
        or "mnemoforge"
    )
    return build_mailbox_get_packet(
        ref=str(args.get("ref") or ""),
        state=str(args.get("state") or "planning"),
        project=project,
        runtime_profile_id=_runtime_profile_id(args, identity_defaults),
        diagnostic=bool(args.get("diagnostic", False)),
        detail=str(args.get("detail") or "compact"),
    )


def _runtime_profile_id(args: dict[str, Any], identity_defaults: dict[str, str]) -> str:
    return str(args.get("runtime_profile_id") or identity_defaults.get("runtime_profile_id") or "unknown_cli")


async def _suppress_repeated_health_nudge(
    packet: dict[str, Any],
    *,
    session_id: str | None,
    scope_key: str = "",
    diagnostic: bool = False,
) -> dict[str, Any]:
    nudge = packet.get("health_nudge")
    if not session_id or not isinstance(nudge, dict) or not nudge:
        return _suppress_repeated_stateless_health_nudge(packet, scope_key=scope_key, diagnostic=diagnostic)
    repeat_key = _health_nudge_repeat_key(packet, nudge, scope_key=scope_key)
    if not repeat_key:
        return packet
    try:
        from app.services.mcp_session_store import get_session_store

        store = get_session_store()
        ctx = await store.get_context(session_id)
        if not isinstance(ctx, dict):
            ctx = {}
            await store.set_context(session_id, ctx)
        seen = [str(item) for item in (ctx.get("health_nudge_seen") or []) if str(item)]
        if repeat_key in seen:
            packet.pop("health_nudge", None)
            if diagnostic:
                packet["health_nudge_suppressed"] = {
                    "reason": "already_shown_in_session",
                    "repeat_key": repeat_key,
                    "next_safe_action": "Use get(query='agent context recall health') if you need the full self-check packet.",
                }
            return packet
        await store.patch_context(session_id, {"health_nudge_seen": [*seen[-19:], repeat_key]})
    except Exception:
        return packet
    return packet


def _suppress_repeated_stateless_health_nudge(
    packet: dict[str, Any],
    *,
    scope_key: str = "",
    diagnostic: bool = False,
) -> dict[str, Any]:
    nudge = packet.get("health_nudge")
    if not isinstance(nudge, dict) or not nudge:
        return packet
    repeat_key = _health_nudge_repeat_key(packet, nudge, scope_key=scope_key)
    if not repeat_key:
        return packet
    now = time.time()
    last_seen = _STATELESS_HEALTH_NUDGE_SEEN.get(repeat_key)
    if last_seen is not None and (now - last_seen) < _HEALTH_NUDGE_STATELESS_COOLDOWN_SECONDS:
        packet.pop("health_nudge", None)
        if diagnostic:
            packet["health_nudge_suppressed"] = {
                "reason": "stateless_cooldown",
                "repeat_key": repeat_key,
                "cooldown_seconds": int(_HEALTH_NUDGE_STATELESS_COOLDOWN_SECONDS),
                "next_safe_action": "Use get(query='agent context recall health') if you need the full self-check packet.",
            }
        return packet
    _STATELESS_HEALTH_NUDGE_SEEN[repeat_key] = now
    return packet


def _health_nudge_scope_key(args: dict[str, Any]) -> str:
    work_token = str(args.get("work_token") or "").strip()
    if work_token:
        digest = hashlib.sha256(work_token.encode("utf-8")).hexdigest()[:16]
        return f"work_token:{digest}"
    task_id = str(args.get("task_id") or "").strip()
    if task_id:
        return f"task:{task_id}"
    return ""


def _health_nudge_repeat_key(packet: dict[str, Any], nudge: dict[str, Any], *, scope_key: str = "") -> str:
    parts = [
        str(scope_key or "").strip(),
        str(packet.get("project") or "").strip(),
        str(packet.get("state") or "").strip(),
        str(nudge.get("cue") or "").strip(),
        str(nudge.get("check") or "").strip(),
    ]
    normalized = [part for part in parts if part]
    return "|".join(normalized)


async def _read_governed_laws(
    *,
    api_base: str,
    project: str,
    dependencies: MailboxReadDependencies,
) -> list[dict[str, Any]]:
    if not dependencies.get or not api_base:
        return []
    try:
        data = await dependencies.get(
            api_base,
            (
                f"/laws?project={quote(project, safe='')}"
                "&status=active&include_promoted=true&limit=20"
            ),
        )
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]
