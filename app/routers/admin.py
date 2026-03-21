from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status, Depends

from app.config import settings

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Task Registry ─────────────────────────────────────────────────────────────

class TaskEntry:
    def __init__(self, name: str, factory):
        self.name = name
        self.factory = factory  # async callable () -> None (infinite loop)
        self.task: asyncio.Task | None = None
        self.started_at: float | None = None
        self.restart_count: int = 0
        self.last_error: str | None = None

    def state(self) -> str:
        if self.task is None:
            return "stopped"
        if self.task.done():
            exc = self.task.exception() if not self.task.cancelled() else None
            return "failed" if exc else ("cancelled" if self.task.cancelled() else "done")
        return "running"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state(),
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else None,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
        }


_task_registry: dict[str, TaskEntry] = {}
_server_start_time = time.time()


def register_task(name: str, factory) -> TaskEntry:
    """Register a background task loop factory. Call once at server startup."""
    entry = TaskEntry(name, factory)
    _task_registry[name] = entry
    return entry


def start_task(entry: TaskEntry) -> asyncio.Task:
    """Start (or restart) a registered background task."""
    async def _wrapper():
        try:
            await entry.factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            entry.last_error = str(e)
            raise

    if entry.task and not entry.task.done():
        entry.task.cancel()

    entry.task = asyncio.create_task(_wrapper())
    entry.started_at = time.time()
    entry.restart_count += 1
    return entry.task


def get_task_registry() -> dict[str, TaskEntry]:
    return _task_registry


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1"}


async def _admin_guard(request: Request) -> None:
    """
    Admin endpoints can expose local logs / SQLite rows.
    Safe default:
      - if API_KEY is set: normal auth middleware protects everything
      - if API_KEY is empty: allow only from localhost
    """
    if settings.api_key:
        return
    if _is_local_request(request):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin endpoints require API_KEY for non-local access",
    )


def _repo_root() -> Path:
    return Path(".").resolve()


def _list_logs() -> list[dict[str, Any]]:
    root = _repo_root()
    logs: list[Path] = []

    logs_dir = root / "logs"
    if logs_dir.exists():
        logs.extend([p for p in logs_dir.glob("*.log") if p.is_file()])

    for extra in ["server.log", "uvicorn.log"]:
        p = root / extra
        if p.exists() and p.is_file():
            logs.append(p)

    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for p in logs:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        try:
            st = rp.stat()
        except OSError:
            continue
        try:
            rel = rp.relative_to(root)
            log_id = rel.as_posix()
        except Exception:
            continue
        items.append(
            {
                "id": log_id,
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),
            }
        )

    items.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
    return items


def _resolve_log_path(log_id: str) -> Path:
    root = _repo_root()
    allowed = {i["id"] for i in _list_logs()}
    if log_id not in allowed:
        raise HTTPException(status_code=404, detail="Unknown log_id")
    p = (root / log_id).resolve()
    try:
        p.relative_to(root)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid log_id")
    return p


def _tail_text(path: Path, *, tail_lines: int, max_bytes: int) -> tuple[str, bool, str]:
    try:
        size = path.stat().st_size
    except OSError:
        raise HTTPException(status_code=404, detail="Log file not found")

    max_bytes = max(1024, min(int(max_bytes), 2_000_000))
    start = max(0, size - max_bytes)
    raw = b""
    try:
        with path.open("rb") as f:
            if start:
                f.seek(start, 0)
            raw = f.read()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read log file: {e}")

    truncated = start > 0
    if truncated:
        # Drop partial first line
        nl = raw.find(b"\n")
        if nl >= 0:
            raw = raw[nl + 1 :]

    encoding = "utf-8"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1251")
            encoding = "cp1251"
        except Exception:
            text = raw.decode("utf-8", errors="replace")
            encoding = "utf-8/replace"

    lines = text.splitlines()
    tail_lines = max(1, min(int(tail_lines), 2000))
    if len(lines) > tail_lines:
        lines = lines[-tail_lines:]
    return ("\n".join(lines), truncated, encoding)


@router.get("/logs")
async def list_logs(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    return {"logs": _list_logs()}


@router.get("/logs/tail")
async def tail_log(
    request: Request,
    log_id: str = Query(..., min_length=1, max_length=300),
    lines: int = Query(200, ge=1, le=2000),
    max_bytes: int = Query(200_000, ge=1024, le=2_000_000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    path = _resolve_log_path(log_id)
    tail, truncated, encoding = _tail_text(path, tail_lines=lines, max_bytes=max_bytes)
    st = path.stat()
    return {
        "log_id": log_id,
        "size": int(st.st_size),
        "mtime": float(st.st_mtime),
        "encoding": encoding,
        "truncated": truncated,
        "tail": tail,
        "client": request.client.host if request.client else "",
    }


def _list_dbs() -> list[dict[str, Any]]:
    root = _repo_root()
    db_dir = root / "qdrant_data"
    if not db_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for p in db_dir.glob("*.db"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({"name": p.name, "size": int(st.st_size), "mtime": float(st.st_mtime)})
    items.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
    return items


def _resolve_db_path(db_name: str) -> Path:
    allowed = {d["name"] for d in _list_dbs()}
    if db_name not in allowed:
        raise HTTPException(status_code=404, detail="Unknown db")
    return (_repo_root() / "qdrant_data" / db_name).resolve()


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/dbs")
async def list_dbs(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    return {"dbs": _list_dbs()}


@router.get("/dbs/tables")
async def list_tables(
    db: str = Query(..., min_length=1, max_length=100),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        conn = _connect_ro(db_path)
        try:
            rows = conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables = [{"name": r["name"], "type": r["type"]} for r in rows]
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"SQLite error: {e}")
    return {"db": db, "tables": tables}


@router.get("/dbs/rows")
async def read_rows(
    db: str = Query(..., min_length=1, max_length=100),
    table: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=50_000),
    search: str = Query("", max_length=200),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        conn = _connect_ro(db_path)
        try:
            known = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
                (table,),
            ).fetchone()
            if not known:
                raise HTTPException(status_code=404, detail="Unknown table")

            cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            col_names = [c["name"] for c in cols] if cols else []

            where = ""
            params: list[Any] = []
            if search and col_names:
                like = f"%{search}%"
                parts = [f'CAST("{c}" AS TEXT) LIKE ?' for c in col_names]
                where = " WHERE " + " OR ".join(parts)
                params.extend([like] * len(parts))

            sql = f'SELECT * FROM "{table}"{where} ORDER BY rowid DESC LIMIT ? OFFSET ?'
            params.extend([int(limit), int(offset)])
            try:
                out = conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                # Some tables/views may not support rowid ordering
                sql2 = f'SELECT * FROM "{table}"{where} LIMIT ? OFFSET ?'
                params2 = params[-2:]
                if where:
                    params2 = params[:-2] + params2
                out = conn.execute(sql2, params2).fetchall()

            rows = []
            for r in out:
                d = dict(r)
                # Keep UI resilient for non-JSON serializable types
                for k, v in list(d.items()):
                    if isinstance(v, (bytes, bytearray)):
                        d[k] = f"<bytes {len(v)}>"
                rows.append(d)
        finally:
            conn.close()
    except HTTPException:
        raise
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"SQLite error: {e}")
    return {
        "db": db,
        "table": table,
        "limit": limit,
        "offset": offset,
        "search": search,
        "columns": col_names,
        "rows": rows,
    }


# ── State Management Endpoints ────────────────────────────────────────────────

@router.get("/status")
async def system_status(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    """Full system state: tasks, connections, memory stats, uptime."""
    tasks = [e.to_dict() for e in _task_registry.values()]

    connections: dict[str, Any] = {}
    try:
        from app.dependencies import get_qdrant
        qdrant = get_qdrant()
        cols = await qdrant._client.get_collections()
        connections["qdrant"] = {"reachable": True, "collections": len(cols.collections)}
    except Exception as e:
        connections["qdrant"] = {"reachable": False, "error": str(e)}

    try:
        from app.dependencies import get_ollama
        vec = await get_ollama().embed("ping")
        connections["ollama"] = {"reachable": bool(vec), "dim": len(vec)}
    except Exception as e:
        connections["ollama"] = {"reachable": False, "error": str(e)}

    memory_stats: dict[str, Any] = {}
    try:
        from app.dependencies import get_qdrant
        from app.config import settings
        qdrant = get_qdrant()
        info = await qdrant._client.get_collection(settings.qdrant_collection_name)
        memory_stats = {"total": info.points_count or 0}
    except Exception:
        pass

    return {
        "uptime_s": round(time.time() - _server_start_time, 1),
        "pid": os.getpid(),
        "tasks": tasks,
        "connections": connections,
        "memory": memory_stats,
    }


@router.get("/tasks")
async def list_tasks(_: None = Depends(_admin_guard)) -> list[dict]:
    """List all registered background tasks and their current state."""
    return [e.to_dict() for e in _task_registry.values()]


@router.post("/tasks/{name}/restart")
async def restart_task(name: str, _: None = Depends(_admin_guard)) -> dict:
    """Restart a named background task without restarting the server."""
    entry = _task_registry.get(name)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"Task '{name}' not found. Available: {list(_task_registry.keys())}",
        )
    start_task(entry)
    return {"restarted": name, "restart_count": entry.restart_count, "state": entry.state()}


@router.post("/reload")
async def soft_reload(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    """
    Soft reload: reconnect Qdrant and Ollama, restart any failed background tasks.
    Does not kill the process — safe to call from watchdog on transient failures.
    """
    results: dict[str, Any] = {}

    try:
        from app.services.qdrant_service import set_qdrant_client
        from qdrant_client import AsyncQdrantClient
        if not settings.qdrant_in_memory:
            new_client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
            await new_client.get_collections()
            set_qdrant_client(new_client)
        results["qdrant"] = "reconnected"
    except Exception as e:
        results["qdrant"] = f"error: {e}"

    try:
        from app.dependencies import get_ollama
        await get_ollama().embed("ping")
        results["ollama"] = "ok"
    except Exception as e:
        results["ollama"] = f"error: {e}"

    restarted = []
    for entry in _task_registry.values():
        if entry.state() in ("failed", "done", "cancelled", "stopped"):
            start_task(entry)
            restarted.append(entry.name)
    results["restarted_tasks"] = restarted

    return {"status": "reloaded", "results": results}

