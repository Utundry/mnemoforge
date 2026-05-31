from __future__ import annotations

import os
from pathlib import Path


SLOPLESSCODE_DATA_DIR_ENV = "SLOPLESSCODE_DATA_DIR"
LEGACY_DATA_DIR_ENV = "MNEMOFORGE_DATA_DIR"
LEGACY_DATA_ROOT = Path("qdrant_data")
CANONICAL_DATA_ROOT = Path("system_data")


def get_system_data_root(*, create: bool = True) -> Path:
    """Return the canonical SloplessCode system-data root.

    The historical default remains qdrant_data for compatibility. New
    deployments can opt in to system_data through SLOPLESSCODE_DATA_DIR without
    breaking older Mnemoforge installs or backup archives.
    """
    for env_name in (SLOPLESSCODE_DATA_DIR_ENV, LEGACY_DATA_DIR_ENV):
        raw = os.getenv(env_name)
        if raw and raw.strip():
            root = Path(raw.strip())
            break
    else:
        root = CANONICAL_DATA_ROOT if CANONICAL_DATA_ROOT.exists() else LEGACY_DATA_ROOT

    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def data_path(*parts: str | os.PathLike[str], create_parent: bool = True) -> Path:
    path = get_system_data_root(create=create_parent).joinpath(*parts)
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def describe_system_data_root() -> dict[str, str | bool]:
    explicit_env = ""
    for env_name in (SLOPLESSCODE_DATA_DIR_ENV, LEGACY_DATA_DIR_ENV):
        raw = os.getenv(env_name)
        if raw and raw.strip():
            explicit_env = env_name
            break
    root = get_system_data_root(create=False)
    return {
        "root": str(root),
        "explicit_env": explicit_env,
        "legacy_default": root == LEGACY_DATA_ROOT,
        "legacy_name": str(LEGACY_DATA_ROOT),
        "canonical_name": str(CANONICAL_DATA_ROOT),
    }
