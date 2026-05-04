from __future__ import annotations

import os
from urllib.parse import urlparse


UNSAFE_OVERRIDE_ENV = "SUPERMEMORY_ALLOW_UNSAFE_LIVE_TESTS"
TEST_TARGETS_ENV = "SUPERMEMORY_DB_TEST_TARGETS"
LIVE_TARGETS_ENV = "SUPERMEMORY_LIVE_TARGETS"


class UnsafeTestTargetError(RuntimeError):
    """Raised when a DB-backed test target points at live storage."""


def _normalize_host(hostname: str | None) -> str:
    return (hostname or "").strip().strip("[]").lower()


def _normalize_port(parsed) -> str:
    if parsed.port:
        return str(parsed.port)
    if parsed.scheme == "https":
        return "443"
    return "80"


def _target_tuple(server_url: str) -> tuple[str, str]:
    parsed = urlparse((server_url or "").strip())
    return _normalize_host(parsed.hostname), _normalize_port(parsed)


def _configured_targets(env_name: str) -> set[tuple[str, str]]:
    raw = os.getenv(env_name, "")
    targets: set[tuple[str, str]] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        targets.add(_target_tuple(item))
    return targets


def is_allowed_db_test_target(server_url: str) -> bool:
    allowed_targets = _configured_targets(TEST_TARGETS_ENV)
    return bool(allowed_targets) and _target_tuple(server_url) in allowed_targets


def is_live_like_target(server_url: str) -> bool:
    live_targets = _configured_targets(LIVE_TARGETS_ENV)
    return bool(live_targets) and _target_tuple(server_url) in live_targets


def assert_db_backed_test_target(server_url: str, *, context: str = "DB-backed test") -> None:
    if is_allowed_db_test_target(server_url):
        return

    if os.getenv(UNSAFE_OVERRIDE_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return

    host, port = _target_tuple(server_url)
    if is_live_like_target(server_url):
        reason = "target looks like the live SuperMemory server"
    elif not _configured_targets(TEST_TARGETS_ENV):
        reason = f"no allowed DB test targets are configured in {TEST_TARGETS_ENV}"
    else:
        reason = "target is outside the declared Docker test contour"

    allowed_targets = _configured_targets(TEST_TARGETS_ENV)
    allowed = ", ".join(f"{host}:{port}" for host, port in sorted(allowed_targets)) or "<none configured>"
    raise UnsafeTestTargetError(
        f"{context} refused to use {server_url!r}: {reason} "
        f"({host}:{port}). Use one of: {allowed}. "
        f"Set {UNSAFE_OVERRIDE_ENV}=1 only with explicit unsafe approval."
    )
