from __future__ import annotations

from pathlib import Path

from app.config import settings


def allowed_roots() -> list[Path]:
    """
    Return list of allowed root directories from config.

    Empty list means "no restriction" (local dev).
    """
    raw = settings.ingest_allowed_roots.strip()
    if not raw:
        return []
    roots: list[Path] = []
    for r in raw.split(","):
        r = r.strip()
        if not r:
            continue
        roots.append(Path(r).expanduser().resolve(strict=False))
    return roots


def is_path_allowed(p: Path) -> bool:
    roots = allowed_roots()
    if not roots:
        return True
    resolved = p.expanduser().resolve(strict=False)
    return any(resolved == r or resolved.is_relative_to(r) for r in roots)


def check_path_allowed(p: Path) -> None:
    """Raise ValueError if path is outside allowed roots (when restriction is active)."""
    roots = allowed_roots()
    if not roots:
        return
    resolved = p.expanduser().resolve(strict=False)
    if not any(resolved == r or resolved.is_relative_to(r) for r in roots):
        raise ValueError(f"Path '{p}' is outside allowed roots: {[str(r) for r in roots]}")

