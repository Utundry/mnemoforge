from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any


class RuntimeOwnershipError(RuntimeError):
    pass


def _detect_runtime_kind(configured: str) -> str:
    value = str(configured or "auto").strip().lower()
    if value and value != "auto":
        return value
    if os.getenv("MNEMOFORGE_DOCKER_SERVICE") or Path("/.dockerenv").exists():
        return "docker"
    return "host"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _is_stale(owner: dict[str, Any], *, stale_seconds: float) -> bool:
    try:
        updated_at = float(owner.get("updated_at") or owner.get("started_at") or 0.0)
    except Exception:
        updated_at = 0.0
    if updated_at <= 0.0:
        return True
    return time.time() - updated_at > max(5.0, float(stale_seconds or 120.0))


class RuntimeOwnershipHandle:
    def __init__(
        self,
        *,
        path: Path,
        payload: dict[str, Any],
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.path = path
        self.payload = payload
        self.heartbeat_seconds = max(1.0, heartbeat_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "RuntimeOwnershipHandle":
        _write_json(self.path, self._fresh_payload())
        self._thread = threading.Thread(target=self._heartbeat_loop, name="runtime-owner-heartbeat", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        current = _read_json(self.path)
        if current and current.get("owner_id") == self.payload.get("owner_id"):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _fresh_payload(self) -> dict[str, Any]:
        payload = dict(self.payload)
        payload["updated_at"] = time.time()
        return payload

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                current = _read_json(self.path)
                if current and current.get("owner_id") not in {None, self.payload.get("owner_id")}:
                    return
                _write_json(self.path, self._fresh_payload())
            except Exception:
                continue


def acquire_runtime_ownership(
    *,
    data_dir: Path = Path("qdrant_data"),
    runtime_kind: str = "auto",
    enabled: bool = True,
    allow_takeover: bool = False,
    stale_seconds: float = 120.0,
) -> RuntimeOwnershipHandle | None:
    if not enabled:
        return None

    data_dir = Path(data_dir)
    owner_path = data_dir / "runtime_owner.json"
    kind = _detect_runtime_kind(runtime_kind)
    hostname = socket.gethostname()
    pid = os.getpid()
    owner_id = f"{kind}:{hostname}:{pid}"
    now = time.time()
    payload = {
        "owner_id": owner_id,
        "runtime_kind": kind,
        "hostname": hostname,
        "pid": pid,
        "started_at": now,
        "updated_at": now,
        "data_dir": str(data_dir.resolve()),
    }

    existing = _read_json(owner_path)
    if existing:
        existing_owner = str(existing.get("owner_id") or "").strip()
        if existing_owner and existing_owner != owner_id and not _is_stale(existing, stale_seconds=stale_seconds):
            if not allow_takeover:
                raise RuntimeOwnershipError(
                    "qdrant_data is already owned by another active runtime: "
                    f"{existing_owner}. Stop that runtime or set "
                    "MNEMOFORGE_RUNTIME_OWNER_ALLOW_TAKEOVER=true for an explicit takeover."
                )

    return RuntimeOwnershipHandle(path=owner_path, payload=payload).start()
