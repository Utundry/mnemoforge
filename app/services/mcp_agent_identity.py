from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

DEFAULT_IDENTITY_PATH = Path(".mnemoforge") / "agent_identity.json"


def resolve_agent_identity_path() -> Path:
    configured = str(os.getenv("MNEMOFORGE_AGENT_IDENTITY_PATH") or "").strip()
    return Path(configured) if configured else DEFAULT_IDENTITY_PATH


def load_or_create_agent_identity(
    *,
    path: Path | None = None,
    client_name: str = "unknown-client",
    runtime_profile_id: str = "unknown_cli",
) -> dict[str, Any]:
    path = path or resolve_agent_identity_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("local_agent_uuid"):
                return data
        except Exception:
            pass
    data = {
        "local_agent_uuid": str(uuid4()),
        "client_name": str(client_name or "unknown-client").strip() or "unknown-client",
        "runtime_profile_id": str(runtime_profile_id or "unknown_cli").strip() or "unknown_cli",
        "version": 1,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def build_agent_fingerprint(
    *,
    workspace_root: str | Path | None = None,
    client_name: str = "",
    model_name: str = "",
    runtime_profile_id: str = "unknown_cli",
    local_agent_uuid: str = "",
) -> str:
    root = Path(workspace_root or os.getcwd()).resolve()
    parts = {
        "workspace_root_hash": _sha256_text(str(root).casefold()),
        "client_name": str(client_name or "unknown-client").strip().casefold(),
        "model_name": str(model_name or "unknown-model").strip().casefold(),
        "runtime_profile_id": str(runtime_profile_id or "unknown_cli").strip().casefold(),
        "local_agent_uuid": str(local_agent_uuid or "").strip(),
    }
    stable = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return "agentfp:" + _sha256_text(stable)[:32]


def build_fingerprint_from_identity(
    identity: dict[str, Any],
    *,
    workspace_root: str | Path | None = None,
    client_name: str = "",
    model_name: str = "",
    runtime_profile_id: str = "",
) -> str:
    return build_agent_fingerprint(
        workspace_root=workspace_root,
        client_name=client_name or str(identity.get("client_name") or ""),
        model_name=model_name,
        runtime_profile_id=runtime_profile_id or str(identity.get("runtime_profile_id") or "unknown_cli"),
        local_agent_uuid=str(identity.get("local_agent_uuid") or ""),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
