from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any


_STARTED_AT = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean(value: Any, *, max_length: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if any(sep in text for sep in ("\\", "/", "\x00", "\r", "\n")):
        return ""
    return text[:max_length]


def _env_first(*names: str, max_length: int = 160) -> str:
    for name in names:
        value = _clean(os.getenv(name), max_length=max_length)
        if value:
            return value
    return ""


def public_server_build_info() -> dict[str, str]:
    """Return safe build metadata for diagnostics without exposing host paths or secrets."""
    data = {
        "service": "mnemoforge",
        "started_at": _STARTED_AT,
        "git_commit": _env_first("MNEMOFORGE_GIT_COMMIT", "GIT_COMMIT", "SOURCE_COMMIT", "COMMIT_SHA", max_length=64),
        "build_tag": _env_first("MNEMOFORGE_BUILD_TAG", "DOCKER_IMAGE_TAG", "BUILD_TAG", max_length=80),
        "image_repository": _env_first("MNEMOFORGE_IMAGE_REPOSITORY", "DOCKER_IMAGE_REPOSITORY", max_length=120),
        "image_digest": _env_first("MNEMOFORGE_IMAGE_DIGEST", "DOCKER_IMAGE_DIGEST", max_length=160),
    }
    return {key: value for key, value in data.items() if value}


def server_build_diagnostics_enabled() -> bool:
    value = str(os.getenv("MNEMOFORGE_EXPOSE_BUILD_INFO") or "").strip().lower()
    return value in {"1", "true", "yes", "on", "diagnostic"}
