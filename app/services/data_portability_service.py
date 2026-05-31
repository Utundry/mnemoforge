from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.services.system_data_root import get_system_data_root

PORTABLE_EXPORT_FORMAT_VERSION = "mnemoforge.portable-export.v1"
DEFAULT_DATA_ROOT = get_system_data_root(create=False)
TEST_STORE_PATH_MARKERS = {
    ".pytest_cache",
    ".pytest-tmp",
    ".pytest_tmp",
    "pytest_temp_local",
}


def _is_test_store_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    if parts & TEST_STORE_PATH_MARKERS:
        return True
    return any(part.lower().startswith(("test_", "tmp_", "pytest_")) for part in path.parts)


def _sqlite_files(root: Path, *, include_test_stores: bool = False) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.db")
        if path.is_file() and (include_test_stores or not _is_test_store_path(path.relative_to(root)))
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return [str(row["name"]) for row in rows]


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _serialize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "__encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    return value


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: _serialize_value(row[key]) for key in row.keys()}


def _count_rows(conn: sqlite3.Connection, table: str, *, project: str | None = None, columns: list[str] | None = None) -> int:
    columns = columns or _columns(conn, table)
    sql = f"SELECT COUNT(*) AS count FROM {_quote_identifier(table)}"
    params: list[Any] = []
    if project and "project" in columns:
        sql += " WHERE project = ?"
        params.append(project)
    row = conn.execute(sql, params).fetchone()
    return int(row["count"] if row else 0)


def _read_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    project: str | None = None,
    columns: list[str] | None = None,
    row_limit: int,
) -> list[dict[str, Any]]:
    columns = columns or _columns(conn, table)
    sql = f"SELECT * FROM {_quote_identifier(table)}"
    params: list[Any] = []
    if project and "project" in columns:
        sql += " WHERE project = ?"
        params.append(project)
    sql += " ORDER BY rowid LIMIT ?"
    params.append(row_limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def build_portable_export_plan(
    *,
    root: Path = DEFAULT_DATA_ROOT,
    project: str | None = None,
    row_limit_per_table: int = 1000,
    include_test_stores: bool = False,
) -> dict[str, Any]:
    root = Path(root)
    stores: list[dict[str, Any]] = []
    for db_path in _sqlite_files(root, include_test_stores=include_test_stores):
        rel_path = db_path.relative_to(root).as_posix()
        store: dict[str, Any] = {
            "path": rel_path,
            "tables": [],
        }
        try:
            with _connect_readonly(db_path) as conn:
                for table in _table_names(conn):
                    columns = _columns(conn, table)
                    row_count = _count_rows(conn, table, columns=columns)
                    exported_count = _count_rows(conn, table, project=project, columns=columns)
                    store["tables"].append(
                        {
                            "name": table,
                            "columns": columns,
                            "row_count": row_count,
                            "exported_count": min(exported_count, row_limit_per_table),
                            "matching_count": exported_count,
                            "project_filter_applied": bool(project and "project" in columns),
                            "truncated": exported_count > row_limit_per_table,
                        }
                    )
        except sqlite3.DatabaseError as exc:
            store["error"] = str(exc)
        stores.append(store)

    return {
        "format_version": PORTABLE_EXPORT_FORMAT_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(root),
        "project": project or "",
        "row_limit_per_table": row_limit_per_table,
        "include_test_stores": include_test_stores,
        "stores": stores,
        "summary": {
            "store_count": len(stores),
            "table_count": sum(len(store.get("tables") or []) for store in stores),
            "exported_rows": sum(
                int(table.get("exported_count") or 0)
                for store in stores
                for table in (store.get("tables") or [])
            ),
            "truncated_tables": sum(
                1
                for store in stores
                for table in (store.get("tables") or [])
                if table.get("truncated")
            ),
        },
    }


def build_portable_export_package(
    *,
    root: Path = DEFAULT_DATA_ROOT,
    project: str | None = None,
    row_limit_per_table: int = 1000,
    include_rows: bool = True,
    include_test_stores: bool = False,
) -> dict[str, Any]:
    plan = build_portable_export_plan(
        root=root,
        project=project,
        row_limit_per_table=row_limit_per_table,
        include_test_stores=include_test_stores,
    )
    stores: list[dict[str, Any]] = []
    root = Path(root)
    for store_plan in plan["stores"]:
        store: dict[str, Any] = {
            "path": store_plan["path"],
            "tables": [],
        }
        if store_plan.get("error"):
            store["error"] = store_plan["error"]
            stores.append(store)
            continue
        db_path = root / store_plan["path"]
        with _connect_readonly(db_path) as conn:
            for table_plan in store_plan.get("tables") or []:
                table = str(table_plan["name"])
                columns = list(table_plan.get("columns") or [])
                table_export = dict(table_plan)
                if include_rows:
                    table_export["rows"] = _read_rows(
                        conn,
                        table,
                        project=project,
                        columns=columns,
                        row_limit=row_limit_per_table,
                    )
                store["tables"].append(table_export)
        stores.append(store)

    package = {
        "manifest": {
            "format_version": plan["format_version"],
            "created_at": plan["created_at"],
            "project": plan["project"],
            "row_limit_per_table": row_limit_per_table,
            "include_test_stores": include_test_stores,
            "summary": plan["summary"],
            "restore_policy": {
                "mode": "preview-required",
                "qdrant": "rebuild-from-sqlite",
                "destructive_import": "not-supported-in-v1",
            },
        },
        "stores": stores,
    }
    # Keep a canonical checksum target shape for future signing without adding a dependency now.
    package["manifest"]["canonical_json_bytes"] = len(json.dumps(package["stores"], sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return package
