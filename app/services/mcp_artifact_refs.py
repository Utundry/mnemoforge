"""Shared helpers for public artifact reference strings."""
from __future__ import annotations

from typing import Any


def artifact_type_from_key(value: Any) -> str:
    text = str(value or "").strip()
    return text.split(":", 1)[0] if ":" in text else ""


def task_id_from_artifact_key(value: Any) -> str:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) >= 3 and parts[0] == "task":
        return ":".join(parts[2:]).strip()
    return ""
