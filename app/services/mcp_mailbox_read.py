from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Any
from urllib.parse import quote

from app.services.mcp_mailbox import build_mailbox_get_packet, build_mailbox_state_packet
from app.services.mcp_host_compatibility import (
    get_mcp_host_compatibility_store,
    resolve_task_continuity_scope,
)
from app.services.task_lease_service import get_task_lease_store, work_handle_to_legacy_context


SessionIdentityCallback = Callable[[str | None], Awaitable[dict[str, str]]]
GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
_HEALTH_NUDGE_STATELESS_COOLDOWN_SECONDS = 300


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
    task_id = str(args.get("task_id") or "").strip()
    work_token = _work_token_from_args(args, project=project, task_id=task_id)
    task_continuity = resolve_task_continuity_scope(
        project=project,
        task_id=task_id,
        work_token=work_token,
    )
    effective_session_id = str(task_continuity.get("session_scope") or session_id or "")
    agent_fingerprint = str(
        args.get("agent_fingerprint")
        or identity_defaults.get("agent_fingerprint")
        or ""
    ).strip()
    compatibility = get_mcp_host_compatibility_store().observe(
        agent_fingerprint=agent_fingerprint,
        session_id=str(session_id or ""),
    )
    if task_continuity:
        compatibility = {
            **compatibility,
            "traits": sorted(set(compatibility.get("traits") or []) | {"task_bound_continuity"}),
            "task_continuity": {
                "active": True,
                "lease_id": task_continuity["lease_id"],
                "expires_at": task_continuity["expires_at"],
            },
        }
    packet = build_mailbox_state_packet(
        state=str(args.get("state") or "planning"),
        project=project,
        runtime_profile_id=_runtime_profile_id(args, identity_defaults),
        diagnostic=bool(args.get("diagnostic", False)),
        detail=str(args.get("detail") or "compact"),
        governed_laws=governed_laws,
        session_id=effective_session_id,
    )
    if bool(args.get("diagnostic", False)):
        packet["host_compatibility"] = compatibility
    _attach_task_scoped_health_nudge(
        packet,
        task_id=task_id,
        task_continuity=task_continuity,
    )
    return await _suppress_repeated_health_nudge(
        packet,
        session_id=effective_session_id,
        compatibility=compatibility,
        scope_key=str(task_continuity.get("scope_key") or _health_nudge_scope_key(args, identity_defaults)),
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


def _work_token_from_args(args: dict[str, Any], *, project: str, task_id: str) -> str:
    work_token = str(args.get("work_token") or "").strip()
    if work_token:
        return work_token
    work_handle = str(args.get("work_handle") or "").strip()
    if not work_handle:
        return ""
    try:
        context = work_handle_to_legacy_context(
            store=get_task_lease_store(),
            work_handle=work_handle,
            project=project,
            task_id=task_id,
        )
    except Exception:
        return ""
    return str(context.get("work_token") or "").strip()


async def _suppress_repeated_health_nudge(
    packet: dict[str, Any],
    *,
    session_id: str | None,
    compatibility: dict[str, Any] | None = None,
    scope_key: str = "",
    diagnostic: bool = False,
) -> dict[str, Any]:
    nudge = packet.get("health_nudge")
    if not isinstance(nudge, dict) or not nudge:
        return packet
    traits = set((compatibility or {}).get("traits") or [])
    if not session_id or "session_churn" in traits:
        return _suppress_repeated_stateless_health_nudge(
            packet,
            scope_key=scope_key,
            diagnostic=diagnostic,
        )
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
            _remove_health_nudge(packet)
            if diagnostic:
                packet["health_nudge_suppressed"] = {
                    "reason": "already_shown_in_session",
                    "repeat_key": repeat_key,
                    "next_safe_action": "Use get(query='agent context recall health') if you need the full self-check packet.",
                }
            return packet
        await store.patch_context(session_id, {"health_nudge_seen": [*seen[-19:], repeat_key]})
        if scope_key:
            get_mcp_host_compatibility_store().check_cooldown(
                scope_key=scope_key,
                event_key=repeat_key,
                cooldown_seconds=_HEALTH_NUDGE_STATELESS_COOLDOWN_SECONDS,
            )
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
    if not repeat_key or not scope_key:
        return packet
    repeated = get_mcp_host_compatibility_store().check_cooldown(
        scope_key=scope_key,
        event_key=repeat_key,
        cooldown_seconds=_HEALTH_NUDGE_STATELESS_COOLDOWN_SECONDS,
    )
    if repeated:
        _remove_health_nudge(packet)
        if diagnostic:
            packet["health_nudge_suppressed"] = {
                "reason": "stateless_cooldown",
                "repeat_key": repeat_key,
                "cooldown_seconds": int(_HEALTH_NUDGE_STATELESS_COOLDOWN_SECONDS),
                "next_safe_action": "Use get(query='agent context recall health') if you need the full self-check packet.",
            }
        return packet
    return packet


def _remove_health_nudge(packet: dict[str, Any]) -> None:
    packet.pop("health_nudge", None)
    cue_packet = packet.get("cue_packet")
    if isinstance(cue_packet, dict):
        cue_packet.pop("health_nudge", None)


def _attach_task_scoped_health_nudge(
    packet: dict[str, Any],
    *,
    task_id: str,
    task_continuity: dict[str, Any],
) -> None:
    if not task_id:
        return
    nudge = packet.get("health_nudge")
    if not isinstance(nudge, dict) or not nudge:
        return
    scoped = {
        **nudge,
        "scope": {
            "kind": "task",
            "task_id": task_id,
            "continuity": "active_claim" if task_continuity else "task_reference",
        },
    }
    if task_continuity:
        scoped["next_safe_action"] = (
            "Answer this self-check for the active task claim; expand the cue only if task context "
            "or authority recall is insufficient."
        )
    packet["health_nudge"] = scoped
    cue_packet = packet.get("cue_packet")
    if isinstance(cue_packet, dict) and isinstance(cue_packet.get("health_nudge"), dict):
        cue_packet["health_nudge"] = scoped


def _health_nudge_scope_key(args: dict[str, Any], identity_defaults: dict[str, str]) -> str:
    agent_fingerprint = str(
        args.get("agent_fingerprint")
        or identity_defaults.get("agent_fingerprint")
        or ""
    ).strip()
    if agent_fingerprint:
        import hashlib

        digest = hashlib.sha256(agent_fingerprint.encode("utf-8")).hexdigest()[:20]
        return f"agent:{digest}"
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
