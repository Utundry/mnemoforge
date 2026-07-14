from __future__ import annotations

from typing import Any


_LIFECYCLE_ROUTE_TOOLS = {
    "record_work_result",
    "record_task_checkpoint",
    "start_task_session",
    "finish_task_session",
}


def compact_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def public_lease_payload(lease: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(lease, dict):
        return {}
    public_lease = dict(lease)
    public_lease.pop("work_token_hash", None)
    public_lease.pop("work_token", None)
    public_lease.pop("work_token_preview", None)
    return compact_public_payload(public_lease)


def public_auto_work_session_payload(result: dict[str, Any]) -> dict[str, Any]:
    work_session = result.get("work_session") if isinstance(result.get("work_session"), dict) else {}
    return compact_public_payload(
        {
            "auto_started": True,
            "project": result.get("project"),
            "task_id": result.get("task_id"),
            "work_id": work_session.get("work_id"),
            "work_handle": result.get("work_handle"),
            "owner_agent": result.get("owner_agent"),
            "owner_session_id": result.get("owner_session_id"),
            "lease": public_lease_payload(result.get("lease") if isinstance(result.get("lease"), dict) else None),
            "next_safe_action": "Reuse this work_handle for later checkpoint or finish operations.",
        }
    )


def build_lifecycle_receipt(*, route_tool: str, result: Any, warnings: list[str] | None = None) -> dict[str, Any]:
    if not isinstance(result, dict) or route_tool not in _LIFECYCLE_ROUTE_TOOLS:
        return {}
    auto_work_session = result.get("auto_work_session") if isinstance(result.get("auto_work_session"), dict) else {}
    public_auto_work_session = dict(auto_work_session)
    if isinstance(public_auto_work_session.get("lease"), dict):
        public_auto_work_session["lease"] = public_lease_payload(public_auto_work_session["lease"])
    raw_lease = result.get("lease") if isinstance(result.get("lease"), dict) else public_auto_work_session.get("lease")
    work_session = result.get("work_session") if isinstance(result.get("work_session"), dict) else {}
    checkpoint = result.get("checkpoint") if isinstance(result.get("checkpoint"), dict) else {}
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    return compact_public_payload(
        {
            "status": result.get("status"),
            "route_tool": route_tool,
            "task_id": result.get("task_id") or target.get("task_id") or checkpoint.get("task_id"),
            "work_id": result.get("work_id") or public_auto_work_session.get("work_id") or work_session.get("work_id"),
            "work_handle": result.get("work_handle") or public_auto_work_session.get("work_handle"),
            "auto_work_session": public_auto_work_session or None,
            "lease": public_lease_payload(raw_lease if isinstance(raw_lease, dict) else None),
            "warnings": warnings or result.get("warnings"),
            "next_safe_action": (
                "Continue with the returned work_handle for later checkpoints or finish_task."
                if public_auto_work_session.get("work_handle")
                else result.get("next_safe_action") or result.get("next_action")
            ),
        }
    )
