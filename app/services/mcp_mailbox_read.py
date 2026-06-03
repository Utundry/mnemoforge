from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Any
from urllib.parse import quote

from app.services.mcp_mailbox import build_mailbox_get_packet, build_mailbox_state_packet


SessionIdentityCallback = Callable[[str | None], Awaitable[dict[str, str]]]
GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]


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
    return build_mailbox_state_packet(
        state=str(args.get("state") or "planning"),
        project=project,
        runtime_profile_id=_runtime_profile_id(args, identity_defaults),
        diagnostic=bool(args.get("diagnostic", False)),
        detail=str(args.get("detail") or "compact"),
        governed_laws=governed_laws,
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
