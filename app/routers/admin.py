from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status, Depends, Body

from app.config import settings
from app.services.code_hardcoding_audit_service import run_code_hardcoding_audit
from app.services.data_integrity_service import (
    build_integrity_forensic_report,
    build_integrity_remediation_outcome,
    build_integrity_repair_plan,
    build_targeted_repair_batch_preview,
    discover_suspect_records,
    get_data_integrity_store,
    queue_recommended_remediation,
    queue_targeted_repair_batch,
    reconcile_completed_remediations,
    run_integrity_audit,
)
from app.services.data_portability_service import (
    build_portable_export_package,
    build_portable_export_plan,
)
from app.services.data_hygiene_service import (
    build_ai_hygiene_resolution_plan,
    build_operator_playbook,
    build_workflow_summary,
    bulk_update_finding_statuses,
    build_delete_dry_run,
    build_reviewed_delete_preview,
    build_retention_report,
    compact_hygiene_remediation,
    findings_for_manual_review,
    get_data_hygiene_store,
    policy_for_dataset_class,
    queue_approved_delete_remediation,
    queue_hygiene_remediation,
    queue_reviewed_delete_remediation,
    reconcile_completed_remediations as reconcile_hygiene_completed_remediations,
    resolve_governed_synthetic_false_positives,
    run_data_hygiene_audit,
)
from app.services.functionality_inventory_service import (
    build_functionality_alpha_config,
    bootstrap_functionality_review_hints,
    build_functionality_inventory,
    build_functionality_release_scope,
    build_functionality_review_dossier,
    build_functionality_review_queue,
    list_functionality_review_hints,
    upsert_functionality_review_hint,
)
from app.services.doc_section_service import backfill_legacy_doc_sections_to_store
from app.services.memoir_service import backfill_legacy_memoirs_to_store
from app.services.publish_readiness_service import build_publish_readiness
from app.services.operational_instincts_service import (
    build_operational_instinct_activation_summary,
    build_operational_instinct_playbook,
    get_active_operational_instincts,
    list_operational_instincts,
    upsert_operational_instinct,
)
from app.services.qdrant_rebuild_service import (
    SUPPORTED_QDRANT_REBUILD_TARGETS,
    reindex_sqlite_backed_qdrant,
)
from app.services.storage_trust_service import build_storage_trust_report
from app.services.system_data_root import get_system_data_root

router = APIRouter(prefix="/admin", tags=["admin"])


def _sync_integrity_remediations_best_effort() -> None:
    try:
        from app.services.job_queue import get_job_queue

        jobs = get_job_queue().list_jobs(limit=500)
        get_data_integrity_store().sync_remediations_from_jobs(jobs)
    except Exception:
        pass


def _sync_data_hygiene_remediations_best_effort() -> None:
    try:
        from app.services.job_queue import get_job_queue

        jobs = get_job_queue().list_jobs(limit=500)
        get_data_hygiene_store().sync_remediations_from_jobs(jobs)
    except Exception:
        pass


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
    db_dir = get_system_data_root()
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
    return (get_system_data_root() / db_name).resolve()


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
    _sync_integrity_remediations_best_effort()
    _sync_data_hygiene_remediations_best_effort()
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

    integrity = get_data_integrity_store().overview()
    data_hygiene = get_data_hygiene_store().overview()
    storage_trust = build_storage_trust_report()

    return {
        "uptime_s": round(time.time() - _server_start_time, 1),
        "pid": os.getpid(),
        "tasks": tasks,
        "connections": connections,
        "memory": memory_stats,
        "integrity": integrity,
        "data_hygiene": data_hygiene,
        "storage_trust": storage_trust,
    }


@router.get("/storage-trust")
async def storage_trust_status(
    project: str | None = Query(None),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_integrity_remediations_best_effort()
    _sync_data_hygiene_remediations_best_effort()
    return build_storage_trust_report(current_project=project)


@router.get("/code-hardcoding-audit")
async def code_hardcoding_audit(
    limit_per_category: int = Query(100, ge=1, le=500),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return run_code_hardcoding_audit(limit_per_category=limit_per_category)


@router.get("/functionality-inventory")
async def functionality_inventory(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    return build_functionality_inventory()


@router.get("/functionality-release-scope")
async def functionality_release_scope(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    return build_functionality_release_scope()


@router.get("/functionality-review-dossier/{module}")
async def functionality_review_dossier(
    module: str,
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    try:
        return build_functionality_review_dossier(module)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/functionality-review-queue")
async def functionality_review_queue(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    return build_functionality_review_queue()


@router.get("/functionality-alpha-config")
async def functionality_alpha_config(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    return build_functionality_alpha_config()


@router.get("/publish-readiness")
async def publish_readiness(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    return build_publish_readiness()


@router.get("/operational-instincts")
async def operational_instincts(
    layer: str | None = Query(None),
    scope_ref: str | None = Query(None),
    family: str | None = Query(None),
    phase: str | None = Query(None),
    active_only: bool = Query(False),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    items = list_operational_instincts(
        layer=layer,
        scope_ref=scope_ref,
        family=family,
        phase=phase,
        active_only=active_only,
    )
    return {"items": items, "total": len(items)}


@router.get("/operational-instincts/active")
async def active_operational_instincts(
    context_type: str = Query(..., min_length=1),
    project_id: str | None = Query(None),
    storage_trust_status: str | None = Query(None),
    code_inspection_recommended: bool = Query(False),
    limit: int = Query(5, ge=1, le=20),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    items = get_active_operational_instincts(
        context_type=context_type,
        project_id=project_id,
        storage_trust_status=storage_trust_status,
        code_inspection_recommended=code_inspection_recommended,
        limit=limit,
    )
    return {"items": items, "total": len(items)}


@router.post("/operational-instincts")
async def upsert_operational_instinct_route(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    required = ("instinct_id", "layer", "rank", "scope", "trigger", "action", "why_it_matters", "failure_if_missing")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")
    return upsert_operational_instinct(
        instinct_id=str(payload["instinct_id"]),
        layer=str(payload["layer"]),
        scope_ref=str(payload.get("scope_ref") or ""),
        family=str(payload.get("family") or "core_bootstrap"),
        phase=str(payload.get("phase") or "general"),
        rank=str(payload["rank"]),
        scope=str(payload["scope"]),
        trigger=str(payload["trigger"]),
        action=str(payload["action"]),
        why_it_matters=str(payload["why_it_matters"]),
        failure_if_missing=str(payload["failure_if_missing"]),
        language=str(payload.get("language") or "en"),
        active=bool(payload.get("active", True)),
        activation_tags=[str(item) for item in payload.get("activation_tags") or []],
    )


@router.get("/operational-instincts/activation-summary")
async def operational_instinct_activation_summary(
    limit: int = Query(200, ge=1, le=2000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_operational_instinct_activation_summary(limit=limit)


@router.get("/operational-instincts/playbook")
async def operational_instinct_playbook(
    family: str = Query(..., min_length=1),
    project_id: str | None = Query(None),
    active_only: bool = Query(True),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_operational_instinct_playbook(
        family=family,
        project_id=project_id,
        active_only=active_only,
    )


@router.get("/functionality-review-hints")
async def functionality_review_hints(
    scope: str = Query("mnemoforge"),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    items = list_functionality_review_hints(scope=scope)
    return {"scope": scope, "items": items, "total": len(items)}


@router.post("/functionality-review-hints")
async def upsert_functionality_review_hint_route(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    if not payload.get("module"):
        raise HTTPException(status_code=400, detail="module is required")
    if not payload.get("status"):
        raise HTTPException(status_code=400, detail="status is required")
    if not payload.get("reason"):
        raise HTTPException(status_code=400, detail="reason is required")
    return upsert_functionality_review_hint(
        scope=str(payload.get("scope") or "mnemoforge"),
        module=str(payload["module"]),
        status=str(payload["status"]),
        reason=str(payload["reason"]),
    )


@router.post("/functionality-review-hints/bootstrap")
async def bootstrap_functionality_review_hints_route(
    scope: str = Query("mnemoforge"),
    overwrite: bool = Query(False),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return bootstrap_functionality_review_hints(scope=scope, overwrite=overwrite)


@router.get("/integrity")
async def integrity_status(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    """Return current data-integrity overview for storage-backed slices."""
    _sync_integrity_remediations_best_effort()
    return get_data_integrity_store().overview()


@router.get("/integrity/remediations")
async def list_integrity_remediations(
    slice_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_integrity_remediations_best_effort()
    store = get_data_integrity_store()
    items = store.list_remediations(slice_id=slice_id, status=status, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/integrity/remediations/{remediation_id}/outcome")
async def integrity_remediation_outcome(
    remediation_id: str,
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_integrity_remediations_best_effort()
    try:
        return build_integrity_remediation_outcome(remediation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/integrity/rules")
async def list_integrity_rules(
    slice_id: str | None = Query(None),
    active_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    items = get_data_integrity_store().list_rules(slice_id=slice_id, active_only=active_only, limit=limit)
    return {"items": items, "total": len(items)}


@router.post("/integrity/rules")
async def upsert_integrity_rule(
    payload: dict[str, Any] = Body(...),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    if not payload.get("slice_id"):
        raise HTTPException(status_code=400, detail="slice_id is required")
    if not payload.get("description"):
        raise HTTPException(status_code=400, detail="description is required")
    return get_data_integrity_store().upsert_rule(
        rule_id=payload.get("rule_id"),
        slice_id=str(payload["slice_id"]),
        description=str(payload["description"]),
        guidance=dict(payload.get("guidance") or {}),
        scope=str(payload.get("scope") or "slice"),
        rule_type=str(payload.get("rule_type") or "guidance"),
        priority=int(payload.get("priority") or 100),
        active=bool(payload.get("active", True)),
    )


@router.get("/integrity/findings")
async def list_integrity_findings(
    slice_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    items = get_data_integrity_store().list_findings(slice_id=slice_id, status=status, limit=limit)
    return {"items": items, "total": len(items)}


@router.post("/integrity/discover/{slice_id}")
async def discover_integrity_findings(
    slice_id: str,
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    try:
        return await discover_suspect_records(slice_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/integrity/forensics/{slice_id}")
async def integrity_forensic_report(
    slice_id: str,
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_integrity_remediations_best_effort()
    return build_integrity_forensic_report(slice_id, limit=limit)


@router.get("/integrity/repair-plan/{slice_id}")
async def integrity_repair_plan(
    slice_id: str,
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_integrity_remediations_best_effort()
    return build_integrity_repair_plan(slice_id, limit=limit)


@router.get("/integrity/repair-batch/{slice_id}")
async def integrity_repair_batch_preview(
    slice_id: str,
    action_type: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_integrity_remediations_best_effort()
    try:
        return build_targeted_repair_batch_preview(slice_id, action_type=action_type, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/integrity/repair-batch/{slice_id}")
async def queue_integrity_repair_batch(
    slice_id: str,
    action_type: str = Query(...),
    requested_by: str = Query("operator"),
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    _sync_integrity_remediations_best_effort()
    try:
        item = await queue_targeted_repair_batch(
            slice_id=slice_id,
            action_type=action_type,
            requested_by=requested_by,
            queue=get_job_queue(),
            limit=limit,
        )
        _sync_integrity_remediations_best_effort()
        return item
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/integrity/findings/{finding_id}/status")
async def update_integrity_finding_status(
    finding_id: str,
    status: str = Query(..., pattern="^(suspect|quarantine_candidate|quarantined|ignored|repaired)$"),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    item = get_data_integrity_store().set_finding_status(finding_id=finding_id, status=status)
    if item is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return item


@router.post("/integrity/reconcile")
async def reconcile_integrity_remediations(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    return await reconcile_completed_remediations(queue=get_job_queue())


@router.post("/integrity/audit")
async def run_integrity_audit_now(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    from app.dependencies import get_qdrant

    return await run_integrity_audit(get_qdrant())


@router.post("/integrity/remediate/{slice_id}")
async def queue_integrity_remediation(
    slice_id: str,
    requested_by: str = Query("admin"),
    discover_if_needed: bool = Query(True),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    try:
        item = await queue_recommended_remediation(
            slice_id=slice_id,
            requested_by=requested_by,
            queue=get_job_queue(),
            discover_if_needed=discover_if_needed,
        )
        return item
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/data-hygiene")
async def data_hygiene_status(
    project: str | None = Query(None),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_data_hygiene_remediations_best_effort()
    return get_data_hygiene_store().overview(current_project=project)


@router.get("/data-portability/export/plan")
async def data_portability_export_plan(
    project: str | None = Query(None),
    row_limit_per_table: int = Query(1000, ge=1, le=10000),
    include_test_stores: bool = Query(False),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_portable_export_plan(
        project=project,
        row_limit_per_table=row_limit_per_table,
        include_test_stores=include_test_stores,
    )


@router.get("/data-portability/export")
async def data_portability_export(
    project: str | None = Query(None),
    row_limit_per_table: int = Query(1000, ge=1, le=10000),
    include_rows: bool = Query(True),
    include_test_stores: bool = Query(False),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_portable_export_package(
        project=project,
        row_limit_per_table=row_limit_per_table,
        include_rows=include_rows,
        include_test_stores=include_test_stores,
    )


@router.get("/data-hygiene/policies")
async def data_hygiene_policies(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    overview = get_data_hygiene_store().overview()
    return {"policies": overview.get("policies", {})}


@router.get("/data-hygiene/findings")
async def list_data_hygiene_findings(
    store_name: str | None = Query(None),
    dataset_class: str | None = Query(None),
    recommended_action: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    items = get_data_hygiene_store().list_findings(
        store_name=store_name,
        dataset_class=dataset_class,
        recommended_action=recommended_action,
        status=status,
        limit=limit,
    )
    return {"items": items, "total": len(items)}


@router.get("/data-hygiene/manual-review")
async def list_data_hygiene_manual_review(
    limit: int = Query(200, ge=1, le=1000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    items = findings_for_manual_review(limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/data-hygiene/workflow")
async def data_hygiene_workflow(
    limit: int = Query(1000, ge=1, le=10000),
    project: str | None = Query(None),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_workflow_summary(limit=limit, current_project=project)


@router.get("/data-hygiene/playbook")
async def data_hygiene_playbook(
    limit: int = Query(1000, ge=1, le=10000),
    project: str | None = Query(None),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_operator_playbook(limit=limit, current_project=project)


@router.get("/data-hygiene/retention-report")
async def data_hygiene_retention_report(
    limit: int = Query(1000, ge=1, le=10000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_retention_report(limit=limit)


@router.get("/data-hygiene/delete-dry-run")
async def data_hygiene_delete_dry_run(
    store_name: str = Query("qdrant_memories"),
    status: str = Query("quarantined", pattern="^(quarantined|quarantine_candidate)$"),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_delete_dry_run(store_name=store_name, status=status, limit=limit)


@router.get("/data-hygiene/reviewed-delete-preview")
async def data_hygiene_reviewed_delete_preview(
    store_name: str = Query("learning_events"),
    status: str = Query("quarantine_candidate", pattern="^(quarantine_candidate)$"),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    return build_reviewed_delete_preview(store_name=store_name, status=status, limit=limit)


@router.get("/data-hygiene/remediations")
async def list_data_hygiene_remediations(
    recommended_action: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    detail: str = Query("compact", pattern="^(compact|full)$"),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    _sync_data_hygiene_remediations_best_effort()
    items = get_data_hygiene_store().list_remediations(
        recommended_action=recommended_action,
        status=status,
        limit=limit,
    )
    if detail == "compact":
        items = [compact_hygiene_remediation(item) for item in items]
    return {"items": items, "total": len(items)}


@router.post("/data-hygiene/findings/{finding_id}/status")
async def update_data_hygiene_finding_status(
    finding_id: str,
    status: str = Query(..., pattern="^(open|ignored|resolved|archived|quarantine_candidate|quarantined|manual_review)$"),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    item = get_data_hygiene_store().set_finding_status(finding_id=finding_id, status=status)
    if item is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    item["policy"] = policy_for_dataset_class(item["dataset_class"])
    return item


@router.post("/data-hygiene/review/bulk-status")
async def bulk_update_data_hygiene_review_status(
    target_status: str = Query(..., pattern="^(manual_review|quarantine_candidate|quarantined|ignored)$"),
    current_status: str | None = Query(None, pattern="^(open|ignored|resolved|archived|quarantine_candidate|quarantined|manual_review)$"),
    dataset_class: str | None = Query(None),
    recommended_action: str | None = Query(None),
    store_name: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    result = bulk_update_finding_statuses(
        target_status=target_status,
        current_status=current_status,
        dataset_class=dataset_class,
        recommended_action=recommended_action,
        store_name=store_name,
        limit=limit,
    )
    result["workflow"] = build_workflow_summary(limit=limit)
    return result


@router.post("/data-hygiene/review/quarantine-synthetic")
async def quarantine_synthetic_delete_candidates(
    current_status: str = Query("open", pattern="^(open|manual_review|quarantine_candidate)$"),
    store_name: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    result = bulk_update_finding_statuses(
        target_status="quarantine_candidate",
        current_status=current_status,
        dataset_class="synthetic_test",
        recommended_action="delete",
        store_name=store_name,
        limit=limit,
    )
    result["workflow"] = build_workflow_summary(limit=1000)
    return result


@router.post("/data-hygiene/review/resolve-governed-synthetic")
async def resolve_governed_synthetic_review_noise(
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    result = resolve_governed_synthetic_false_positives(limit=limit)
    result["workflow"] = build_workflow_summary(limit=1000)
    return result


@router.post("/data-hygiene/audit")
async def run_data_hygiene_audit_now(
    memory_limit: int = Query(1000, ge=1, le=5000),
    event_limit: int = Query(1000, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.dependencies import get_qdrant

    return await run_data_hygiene_audit(
        get_qdrant(),
        memory_limit=memory_limit,
        event_limit=event_limit,
    )


@router.post("/data-hygiene/ai-resolve")
async def ai_resolve_data_hygiene(
    auto_apply_safe: bool = Query(True),
    requested_by: str = Query("ai-operator"),
    limit: int = Query(500, ge=1, le=5000),
    sample_size: int = Query(10, ge=1, le=50),
    detail: str = Query("compact", pattern="^(compact|full)$"),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    queue = get_job_queue()
    _sync_data_hygiene_remediations_best_effort()
    plan = build_ai_hygiene_resolution_plan(limit=limit, sample_size=sample_size)

    queued_remediations: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    if auto_apply_safe:
        for candidate in plan.get("safe_remediation_candidates", []):
            open_findings = int(candidate.get("open_findings") or 0)
            if open_findings <= 0:
                continue
            action = str(candidate.get("recommended_action") or "")
            store_name = str(candidate.get("store_name") or "")
            if not candidate.get("auto_apply_allowed"):
                skipped_candidates.append(
                    {
                        "recommended_action": action,
                        "store_name": store_name,
                        "open_findings": open_findings,
                        "reason": str(candidate.get("blocked_reason") or "blocked"),
                    }
                )
                continue
            try:
                item = await queue_hygiene_remediation(
                    recommended_action=action,
                    requested_by=requested_by,
                    queue=queue,
                    store_name=store_name or None,
                    limit=min(limit, open_findings),
                )
                queued_remediations.append(item)
            except ValueError as exc:
                skipped_candidates.append(
                    {
                        "recommended_action": action,
                        "store_name": store_name,
                        "open_findings": open_findings,
                        "reason": str(exc),
                    }
                )

    _sync_data_hygiene_remediations_best_effort()
    reconcile = await reconcile_hygiene_completed_remediations(queue=queue)
    updated_plan = build_ai_hygiene_resolution_plan(limit=limit, sample_size=sample_size)
    response_remediations = queued_remediations
    if detail == "compact":
        response_remediations = [compact_hygiene_remediation(item) for item in queued_remediations]

    return {
        "auto_apply_safe": auto_apply_safe,
        "requested_by": requested_by,
        "plan": updated_plan,
        "queued_remediations": response_remediations,
        "queued_count": len(queued_remediations),
        "skipped_candidates": skipped_candidates,
        "reconcile": reconcile,
    }


@router.post("/data-hygiene/reconcile")
async def reconcile_data_hygiene_remediations(_: None = Depends(_admin_guard)) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    return await reconcile_hygiene_completed_remediations(queue=get_job_queue())


@router.post("/data-hygiene/remediate")
async def queue_data_hygiene_remediation(
    recommended_action: str = Query(..., pattern="^(exclude-from-learning|archive)$"),
    store_name: str | None = Query(None),
    dataset_class: str | None = Query(None),
    requested_by: str = Query("admin"),
    limit: int = Query(500, ge=1, le=5000),
    detail: str = Query("compact", pattern="^(compact|full)$"),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    item = await queue_hygiene_remediation(
        recommended_action=recommended_action,
        requested_by=requested_by,
        queue=get_job_queue(),
        store_name=store_name,
        dataset_class=dataset_class,
        limit=limit,
    )
    _sync_data_hygiene_remediations_best_effort()
    if detail == "compact":
        return compact_hygiene_remediation(item)
    return item


@router.post("/data-hygiene/remediate-reviewed-delete")
async def queue_data_hygiene_reviewed_delete(
    requested_by: str = Query("admin"),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    item = await queue_reviewed_delete_remediation(
        requested_by=requested_by,
        queue=get_job_queue(),
        limit=limit,
    )
    _sync_data_hygiene_remediations_best_effort()
    return item


@router.post("/data-hygiene/remediate-approved-delete")
async def queue_data_hygiene_approved_delete(
    requested_by: str = Query("admin"),
    store_name: str = Query("qdrant_memories"),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.services.job_queue import get_job_queue

    item = await queue_approved_delete_remediation(
        requested_by=requested_by,
        queue=get_job_queue(),
        store_name=store_name,
        limit=limit,
    )
    _sync_data_hygiene_remediations_best_effort()
    return item


@router.post("/memoirs/backfill")
async def backfill_legacy_memoirs(
    limit: int = Query(500, ge=1, le=5000),
    dry_run: bool = Query(True),
    rewrite_qdrant_refs: bool = Query(False),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.dependencies import get_qdrant

    return await backfill_legacy_memoirs_to_store(
        get_qdrant()._client,
        settings.qdrant_collection_name,
        limit=limit,
        rewrite_qdrant_refs=rewrite_qdrant_refs,
        dry_run=dry_run,
    )


@router.post("/docs/backfill-doc-sections")
async def backfill_legacy_doc_sections(
    limit: int = Query(500, ge=1, le=5000),
    dry_run: bool = Query(True),
    rewrite_qdrant_refs: bool = Query(False),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.dependencies import get_qdrant

    return await backfill_legacy_doc_sections_to_store(
        get_qdrant()._client,
        settings.qdrant_collection_name,
        limit=limit,
        rewrite_qdrant_refs=rewrite_qdrant_refs,
        dry_run=dry_run,
    )


@router.post("/qdrant/reindex-from-sqlite")
async def reindex_qdrant_from_sqlite(
    limit: int = Query(500, ge=1, le=5000),
    dry_run: bool = Query(True),
    targets: list[str] = Query([]),
    _: None = Depends(_admin_guard),
) -> dict[str, Any]:
    from app.dependencies import get_ollama, get_qdrant

    requested = [item for item in targets if item in SUPPORTED_QDRANT_REBUILD_TARGETS]
    return await reindex_sqlite_backed_qdrant(
        qdrant=get_qdrant(),
        ollama=get_ollama(),
        targets=requested,
        limit=limit,
        dry_run=dry_run,
    )


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
