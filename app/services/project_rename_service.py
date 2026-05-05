from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.project_identity_service import ProjectIdentityStore
from scripts.migrate_project_identity import migrate_sqlite_file

PROJECT_DATA_DIR = Path("qdrant_data")
PROJECT_RENAME_BACKUP_DIR = PROJECT_DATA_DIR / "identity_migration_backups"


def _clean_project_id(value: object) -> str:
    return str(value or "").strip()[:128]


def rename_project_identity(
    *,
    old_project_id: str,
    new_project_id: str,
    apply: bool = False,
    include_text: bool = False,
    ensure_alias: bool = True,
    reason: str = "",
    data_dir: Path = PROJECT_DATA_DIR,
    backup_dir: Path = PROJECT_RENAME_BACKUP_DIR,
) -> dict[str, Any]:
    old = _clean_project_id(old_project_id)
    new = _clean_project_id(new_project_id)
    if not old or not new:
        raise ValueError("old_project_id and new_project_id are required")
    if old == new:
        raise ValueError("old_project_id and new_project_id must be different")

    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)
    mappings = [(old, new)]
    report: dict[str, Any] = {
        "operation": "project_rename",
        "mode": "apply" if apply else "dry-run",
        "old_project_id": old,
        "new_project_id": new,
        "canonical_project_id": new,
        "include_text": bool(include_text),
        "ensure_alias": bool(ensure_alias),
        "mappings": [{"alias": old, "project_id": new}],
        "sqlite_files": {},
        "qdrant": {
            "status": "not_included",
            "reason": "Qdrant payload canonicalization is handled by the CLI migration until it has an async job wrapper.",
        },
    }

    if ensure_alias:
        if apply:
            alias_store = ProjectIdentityStore(data_dir / "project_identity.db")
            try:
                canonical = alias_store.upsert_alias(
                    alias=new,
                    project_id=new,
                    reason="canonical project id",
                )
                alias = alias_store.upsert_alias(
                    alias=old,
                    project_id=new,
                    reason=reason or "project rename",
                )
                report["sqlite_files"]["project_identity.db"] = {
                    "exists": True,
                    "changed_rows": 2,
                    "changed_cells": 2,
                    "aliases": [canonical, alias],
                }
            finally:
                alias_store.close()
        else:
            report["sqlite_files"]["project_identity.db"] = {
                "exists": (data_dir / "project_identity.db").exists(),
                "changed_rows": 2,
                "changed_cells": 2,
                "dry_run_aliases": [
                    {"alias": new, "project_id": new, "reason": "canonical project id"},
                    {"alias": old, "project_id": new, "reason": reason or "project rename"},
                ],
            }

    for db_path in sorted(data_dir.glob("*.db")):
        if ensure_alias and db_path.name == "project_identity.db":
            continue
        report["sqlite_files"][db_path.name] = migrate_sqlite_file(
            db_path,
            old=old,
            new=new,
            mappings=mappings,
            apply=apply,
            include_text=include_text,
            backup_dir=backup_dir,
        )

    sqlite_reports = list(report["sqlite_files"].values())
    report["summary"] = {
        "sqlite_files_seen": len(sqlite_reports),
        "sqlite_files_with_changes": sum(1 for item in sqlite_reports if int(item.get("changed_rows") or 0) > 0),
        "changed_rows": sum(int(item.get("changed_rows") or 0) for item in sqlite_reports),
        "changed_cells": sum(int(item.get("changed_cells") or 0) for item in sqlite_reports),
        "backups_created": sum(1 for item in sqlite_reports if item.get("backup")),
    }
    return report
