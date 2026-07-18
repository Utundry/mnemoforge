from __future__ import annotations

import copy
import logging
import os
from typing import Any

from app.services.data_integrity_service import (
    build_auto_discovery_guard,
    build_auto_remediation_guard,
    get_data_integrity_store,
    maybe_auto_discover_slice,
    queue_recommended_remediation,
)
from app.services.mcp_workflow_specs import WorkflowSpecError, load_named_json_spec

logger = logging.getLogger(__name__)

_POLICY_SPEC_PATH = "integrity_autorepair.json"
_ACTIVE_REMEDIATION_STATUSES = {"queued", "running"}
_TERMINAL_REMEDIATION_STATUSES = {"done", "failed"}


def load_integrity_autorepair_policy() -> dict[str, Any]:
    policy = copy.deepcopy(load_named_json_spec(_POLICY_SPEC_PATH))
    defaults = policy.get("defaults")
    if not isinstance(defaults, dict):
        raise WorkflowSpecError("Integrity autorepair policy defaults must be an object")
    slices = policy.get("slices")
    if not isinstance(slices, dict):
        raise WorkflowSpecError("Integrity autorepair policy slices must be an object")
    safe_action_types = policy.get("safe_action_types")
    if not isinstance(safe_action_types, list) or not all(
        isinstance(item, str) for item in safe_action_types
    ):
        raise WorkflowSpecError("Integrity autorepair policy safe_action_types must be a string list")
    return policy


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def integrity_autorepair_enabled(policy: dict[str, Any] | None = None) -> bool:
    env_value = _env_bool("INTEGRITY_AUTO_REMEDIATE")
    if env_value is not None:
        return env_value
    active_policy = policy or load_integrity_autorepair_policy()
    return bool(active_policy.get("enabled", False))


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _non_negative_float(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return max(0.0, float(default))


def _slice_policy(policy: dict[str, Any], slice_id: str) -> dict[str, Any] | None:
    slices = policy.get("slices") or {}
    item = slices.get(slice_id)
    return dict(item) if isinstance(item, dict) else None


def build_integrity_autorepair_decision(
    slice_id: str,
    *,
    policy: dict[str, Any] | None = None,
    cooldown_seconds: float | None = None,
) -> dict[str, Any]:
    active_policy = policy or load_integrity_autorepair_policy()
    defaults = dict(active_policy.get("defaults") or {})
    slice_policy = _slice_policy(active_policy, slice_id)
    if not integrity_autorepair_enabled(active_policy):
        return {"slice_id": slice_id, "allowed": False, "reason": "policy_disabled"}
    if not slice_policy or not bool(slice_policy.get("enabled", False)):
        return {"slice_id": slice_id, "allowed": False, "reason": "slice_not_enabled"}

    action_type = str(slice_policy.get("action_type") or "").strip()
    safe_action_types = {
        str(item).strip()
        for item in active_policy.get("safe_action_types") or []
        if str(item).strip()
    }
    if action_type not in safe_action_types:
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "unsafe_action_type",
            "action_type": action_type,
        }

    store = get_data_integrity_store()
    remediations = store.list_remediations(slice_id=slice_id, limit=50)
    active = [item for item in remediations if item.get("status") in _ACTIVE_REMEDIATION_STATUSES]
    if active:
        latest_active = max(active, key=lambda item: item.get("started_at") or item.get("created_at") or 0.0)
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "active_remediation_exists",
            "action_type": action_type,
            "active_remediation_id": latest_active.get("remediation_id"),
            "active_status": latest_active.get("status"),
        }

    requested_by = str(active_policy.get("requested_by") or "auto_integrity")
    max_attempts = _positive_int(defaults.get("max_auto_attempts"), 3)
    attempts = [
        item
        for item in remediations
        if item.get("action_type") == action_type
        and item.get("requested_by") == requested_by
        and item.get("status") in _TERMINAL_REMEDIATION_STATUSES
    ]
    if len(attempts) >= max_attempts:
        return {
            "slice_id": slice_id,
            "allowed": False,
            "reason": "attempt_limit_reached",
            "action_type": action_type,
            "attempts": len(attempts),
            "max_attempts": max_attempts,
        }

    guard_cooldown = _non_negative_float(
        cooldown_seconds,
        _non_negative_float(defaults.get("cooldown_seconds"), 3600.0),
    )
    guard = build_auto_remediation_guard(slice_id, cooldown_seconds=guard_cooldown)
    if not guard.get("allowed"):
        decision = dict(guard)
        decision["action_type"] = action_type
        return decision

    return {
        "slice_id": slice_id,
        "allowed": True,
        "reason": "policy_and_guard_allowed",
        "action_type": action_type,
        "requested_by": requested_by,
        "cooldown_seconds": guard_cooldown,
        "attempts": len(attempts),
        "max_attempts": max_attempts,
        "batch_limit": _positive_int(
            slice_policy.get("payload", {}).get("limit"),
            _positive_int(defaults.get("batch_limit"), 100),
        ),
        "guard": guard,
    }


async def maybe_queue_integrity_autorepairs(
    *,
    queue,
    discovery_limit: int | None = None,
    discovery_cooldown_seconds: float | None = None,
    remediation_cooldown_seconds: float | None = None,
) -> dict[str, Any]:
    policy = load_integrity_autorepair_policy()
    enabled = integrity_autorepair_enabled(policy)
    store = get_data_integrity_store()
    overview = store.overview()
    result: dict[str, Any] = {
        "enabled": enabled,
        "queued": [],
        "skipped": [],
        "discovered": [],
    }
    if not enabled:
        return result

    defaults = dict(policy.get("defaults") or {})
    policy_slices = policy.get("slices") or {}
    actionable = [
        slice_id
        for slice_id in overview.get("actionable_slices", [])
        if slice_id in policy_slices
    ]
    requested_by = str(policy.get("requested_by") or "auto_integrity")
    resolved_discovery_limit = _positive_int(discovery_limit, _positive_int(defaults.get("discovery_limit"), 50))
    resolved_discovery_cooldown = _non_negative_float(
        discovery_cooldown_seconds,
        _non_negative_float(defaults.get("discovery_cooldown_seconds"), 3600.0),
    )
    resolved_remediation_cooldown = _non_negative_float(
        remediation_cooldown_seconds,
        _non_negative_float(defaults.get("cooldown_seconds"), 3600.0),
    )

    for slice_id in actionable:
        slice_policy = _slice_policy(policy, slice_id) or {}
        if bool(defaults.get("discover_before_queue", True)):
            discovery_guard = build_auto_discovery_guard(slice_id, cooldown_seconds=resolved_discovery_cooldown)
            if discovery_guard.get("allowed"):
                try:
                    discovery = await maybe_auto_discover_slice(
                        slice_id,
                        limit=resolved_discovery_limit,
                        cooldown_seconds=resolved_discovery_cooldown,
                    )
                    if discovery.get("performed"):
                        result["discovered"].append(discovery)
                except Exception as exc:
                    logger.warning("Integrity autorepair discovery failed for %s: %s", slice_id, exc)
                    result["skipped"].append({"slice_id": slice_id, "reason": "discovery_failed", "error": str(exc)})
                    continue

        decision = build_integrity_autorepair_decision(
            slice_id,
            policy=policy,
            cooldown_seconds=resolved_remediation_cooldown,
        )
        if not decision.get("allowed"):
            result["skipped"].append(decision)
            continue
        try:
            queued = await queue_recommended_remediation(
                slice_id=slice_id,
                requested_by=requested_by,
                queue=queue,
                discover_if_needed=False,
                discovery_limit=resolved_discovery_limit,
            )
            result["queued"].append(queued)
            logger.info(
                "Integrity autorepair queued: slice=%s action=%s remediation=%s job=%s reason=%s",
                slice_id,
                decision.get("action_type"),
                queued.get("remediation_id"),
                queued.get("job_id"),
                slice_policy.get("reason", ""),
            )
        except ValueError as exc:
            result["skipped"].append({"slice_id": slice_id, "reason": "no_background_remediation", "error": str(exc)})
        except Exception as exc:
            logger.warning("Integrity autorepair queue failed for %s: %s", slice_id, exc)
            result["skipped"].append({"slice_id": slice_id, "reason": "queue_failed", "error": str(exc)})
    return result
