from __future__ import annotations

from app.config import settings


CAPABILITY_TAG_PREFIX = "requires-capability:"


def configured_project_capabilities() -> set[str]:
    return {
        item.strip()
        for item in str(settings.project_capabilities or "").split(",")
        if item.strip()
    }


def evaluate_capability_tags(tags: list[str] | None) -> dict[str, object]:
    required = sorted(
        {
            str(tag)[len(CAPABILITY_TAG_PREFIX):].strip()
            for tag in tags or []
            if str(tag).startswith(CAPABILITY_TAG_PREFIX)
            and str(tag)[len(CAPABILITY_TAG_PREFIX):].strip()
        }
    )
    available = configured_project_capabilities()
    missing = [capability for capability in required if capability not in available]
    return {
        "status": "unavailable" if missing else "available",
        "required_capabilities": required,
        "missing_capabilities": missing,
        "reason": (
            f"Required project capabilities are not installed in this runtime: {', '.join(missing)}."
            if missing
            else ""
        ),
    }
