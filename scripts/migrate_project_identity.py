#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.services.project_identity_service import ProjectIdentityStore
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels


STRUCTURED_TEXT_COLUMNS = {
    "project",
    "project_id",
    "tags",
    "metadata",
    "meta",
    "meta_json",
    "topic_path",
    "artifact_key",
    "linked_artifact_key",
    "task_artifact_key",
    "improvement_artifact_key",
    "context_signature",
    "record_task_checkpoint_args_json",
    "validation_report_json",
    "source_evidence_json",
    "source_span_ids_json",
    "metrics_json",
    "report_history",
    "evidence_refs_json",
    "structured_fields",
    "structured_fields_json",
}

TEXT_HISTORY_COLUMNS = {
    "content",
    "description",
    "title",
    "summary",
    "preview",
    "observation",
    "why",
    "reason",
    "statement",
    "rationale",
    "promotion_hint",
    "last_review_reason",
    "last_status_action_reason",
}


@dataclass
class MigrationReport:
    mode: str
    old: str
    new: str
    mappings: list[dict[str, str]] = field(default_factory=list)
    sqlite_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    qdrant: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "old": self.old,
            "new": self.new,
            "mappings": self.mappings,
            "sqlite_files": self.sqlite_files,
            "qdrant": self.qdrant,
        }


def _replace_text(value: str, old: str, new: str) -> str:
    replacements = {
        old: new,
        old.lower(): new.lower(),
        old.upper(): new.upper(),
        f"project:{old}": f"project:{new}",
        f"project:{old.lower()}": f"project:{new.lower()}",
        f":{old}:": f":{new}:",
        f":{old.lower()}:": f":{new.lower()}:",
    }
    result = value
    for before, after in replacements.items():
        result = result.replace(before, after)
    return result


def _apply_mappings_to_text(value: str, mappings: list[tuple[str, str]]) -> str:
    result = value
    for old, new in mappings:
        result = _replace_text(result, old, new)
    return result


def _migrate_jsonish(value: Any, mappings: list[tuple[str, str]], *, include_text: bool) -> Any:
    if isinstance(value, dict):
        return {
            _migrate_jsonish(key, mappings, include_text=True): _migrate_jsonish(
                item,
                mappings,
                include_text=include_text,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_migrate_jsonish(item, mappings, include_text=include_text) for item in value]
    if isinstance(value, str):
        if include_text or any(_looks_structured_string(value, old) for old, _new in mappings):
            return _apply_mappings_to_text(value, mappings)
        return value
    return value


def _looks_structured_string(value: str, old: str) -> bool:
    lower = value.lower()
    old_lower = old.lower()
    return (
        value == old
        or lower == old_lower
        or f"project:{old_lower}" in lower
        or f":{old_lower}:" in lower
        or lower.startswith(f"{old_lower}/")
        or f"/{old_lower}/" in lower
    )


def migrate_value(value: Any, old: str, new: str, *, include_text: bool) -> Any:
    return migrate_value_with_mappings(value, [(old, new)], include_text=include_text)


def migrate_value_with_mappings(value: Any, mappings: list[tuple[str, str]], *, include_text: bool) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = None
        if parsed is not None:
            migrated = _migrate_jsonish(parsed, mappings, include_text=include_text)
            if migrated != parsed:
                return json.dumps(migrated, ensure_ascii=False, sort_keys=True)
            return value
    if include_text or any(_looks_structured_string(value, old) for old, _new in mappings):
        return _apply_mappings_to_text(value, mappings)
    return value


def _should_migrate_column(column: str, *, include_text: bool) -> bool:
    normalized = column.lower()
    if normalized in STRUCTURED_TEXT_COLUMNS:
        return True
    if include_text and normalized in TEXT_HISTORY_COLUMNS:
        return True
    if normalized.endswith("_json") or normalized.endswith("_artifact_key"):
        return True
    return include_text


def _backup_sqlite(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{db_path.name}.{stamp}.bak"
    shutil.copy2(db_path, target)
    return target


def migrate_sqlite_file(
    db_path: Path,
    *,
    old: str,
    new: str,
    mappings: list[tuple[str, str]] | None = None,
    apply: bool,
    include_text: bool,
    backup_dir: Path,
) -> dict[str, Any]:
    mappings = mappings or [(old, new)]
    if not db_path.exists():
        return {"exists": False, "changed_rows": 0, "changed_cells": 0}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    changed_rows = 0
    changed_cells = 0
    table_reports: dict[str, Any] = {}
    backup_path = ""
    try:
        tables = [
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            if not str(row["name"]).startswith("sqlite_")
        ]
        for table in tables:
            columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
            pk_columns = [str(row["name"]) for row in columns if int(row["pk"] or 0) > 0]
            text_columns = [
                str(row["name"])
                for row in columns
                if _should_migrate_column(str(row["name"]), include_text=include_text)
            ]
            if not pk_columns or not text_columns:
                continue
            selected_columns = list(dict.fromkeys(pk_columns + text_columns))
            rows = conn.execute(
                f"SELECT {', '.join(selected_columns)} FROM {table}"
            ).fetchall()
            table_changed_rows = 0
            table_changed_cells = 0
            for row in rows:
                patch: dict[str, Any] = {}
                for column in text_columns:
                    old_value = row[column]
                    new_value = migrate_value_with_mappings(old_value, mappings, include_text=include_text)
                    if new_value != old_value:
                        patch[column] = new_value
                if not patch:
                    continue
                table_changed_rows += 1
                table_changed_cells += len(patch)
                if apply:
                    where = " AND ".join(f"{column} = ?" for column in pk_columns)
                    values = list(patch.values()) + [row[column] for column in pk_columns]
                    conn.execute(
                        f"UPDATE {table} SET {', '.join(f'{column} = ?' for column in patch)} WHERE {where}",
                        values,
                    )
            if table_changed_rows:
                table_reports[table] = {
                    "changed_rows": table_changed_rows,
                    "changed_cells": table_changed_cells,
                    "columns": sorted(text_columns),
                }
                changed_rows += table_changed_rows
                changed_cells += table_changed_cells
        if apply and changed_rows:
            backup_path = str(_backup_sqlite(db_path, backup_dir))
            conn.commit()
    finally:
        conn.close()
    return {
        "exists": True,
        "changed_rows": changed_rows,
        "changed_cells": changed_cells,
        "backup": backup_path,
        "tables": table_reports,
    }


def _migrate_payload(payload: dict[str, Any], mappings: list[tuple[str, str]], *, include_text: bool) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for key, value in payload.items():
        key_lower = str(key).lower()
        should_include_text = include_text or key_lower not in TEXT_HISTORY_COLUMNS
        if key_lower in TEXT_HISTORY_COLUMNS and not include_text:
            continue
        migrated = _migrate_jsonish(value, mappings, include_text=should_include_text)
        if migrated != value:
            patch[key] = migrated
    return patch


async def migrate_qdrant(
    *,
    old: str,
    new: str,
    mappings: list[tuple[str, str]] | None = None,
    apply: bool,
    include_text: bool,
    limit: int,
) -> dict[str, Any]:
    mappings = mappings or [(old, new)]
    client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    collection = settings.qdrant_collection_name
    scanned = 0
    matched = 0
    changed = 0
    next_offset = None
    try:
        while True:
            points, next_offset = await client.scroll(
                collection_name=collection,
                limit=min(256, max(1, limit - scanned)) if limit else 256,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                break
            for point in points:
                scanned += 1
                payload = dict(point.payload or {})
                blob = json.dumps(payload, ensure_ascii=False)
                if not any(old_name.lower() in blob.lower() for old_name, _new_name in mappings):
                    continue
                matched += 1
                patch = _migrate_payload(payload, mappings, include_text=include_text)
                if not patch:
                    continue
                changed += 1
                if apply:
                    await client.set_payload(
                        collection_name=collection,
                        payload=patch,
                        points=[point.id],
                    )
                if limit and scanned >= limit:
                    break
            if next_offset is None or (limit and scanned >= limit):
                break
    finally:
        await client.close()
    return {
        "collection": collection,
        "scanned": scanned,
        "matched_old_name": matched,
        "changed_points": changed,
    }


def _load_mapping_file(path: str) -> list[tuple[str, str]]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("aliases") or data.get("mappings") or data
    mappings: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for alias, project_id in data.items():
            mappings.append((str(alias).strip(), str(project_id).strip()))
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            alias = str(item.get("alias") or item.get("old") or "").strip()
            project_id = str(item.get("project_id") or item.get("canonical") or item.get("new") or "").strip()
            if alias and project_id:
                mappings.append((alias, project_id))
    return [(old, new) for old, new in mappings if old and new and old != new]


def _load_alias_store_mappings(data_dir: str) -> list[tuple[str, str]]:
    db_path = Path(data_dir) / "project_identity.db"
    if not db_path.exists():
        return []
    store = ProjectIdentityStore(db_path)
    try:
        rows = store.list_aliases()
        return [
            (str(row["alias"]), str(row["project_id"]))
            for row in rows
            if str(row.get("alias") or "") and str(row.get("project_id") or "") and row["alias"] != row["project_id"]
        ]
    finally:
        store.close()


def _resolve_mappings(args: argparse.Namespace, old: str, new: str) -> list[tuple[str, str]]:
    mappings = _load_mapping_file(args.mapping_file)
    if args.use_alias_table:
        mappings.extend(_load_alias_store_mappings(args.data_dir))
    if not mappings:
        mappings.append((old, new))
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for alias, project_id in mappings:
        item = (alias.strip(), project_id.strip())
        if not item[0] or not item[1] or item[0] == item[1] or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    deduped.sort(key=lambda item: len(item[0]), reverse=True)
    return deduped


async def run(args: argparse.Namespace) -> MigrationReport:
    old = args.old.strip()
    new = args.new.strip()
    mappings = _resolve_mappings(args, old, new)
    report = MigrationReport(
        mode="apply" if args.apply else "dry-run",
        old=old,
        new=new,
        mappings=[{"alias": alias, "project_id": project_id} for alias, project_id in mappings],
    )
    backup_dir = Path(args.backup_dir)
    if args.ensure_alias:
        if args.apply:
            alias_store = ProjectIdentityStore(Path(args.data_dir) / "project_identity.db")
            try:
                alias = alias_store.upsert_alias(
                    alias=old,
                    project_id=new,
                    reason=args.alias_reason or "project identity migration",
                )
                canonical = alias_store.upsert_alias(
                    alias=new,
                    project_id=new,
                    reason="canonical project id",
                )
                report.sqlite_files["project_identity.db"] = {
                    "exists": True,
                    "changed_rows": 2,
                    "changed_cells": 2,
                    "aliases": [canonical, alias],
                }
            finally:
                alias_store.close()
        else:
            report.sqlite_files["project_identity.db"] = {
                "exists": (Path(args.data_dir) / "project_identity.db").exists(),
                "changed_rows": 2,
                "changed_cells": 2,
                "dry_run_aliases": [
                    {"alias": new, "project_id": new, "reason": "canonical project id"},
                    {"alias": old, "project_id": new, "reason": args.alias_reason or "project identity migration"},
                ],
            }
    for db_path in sorted(Path(args.data_dir).glob("*.db")):
        report.sqlite_files[db_path.name] = migrate_sqlite_file(
            db_path,
            old=old,
            new=new,
            mappings=mappings,
            apply=args.apply,
            include_text=args.include_text,
            backup_dir=backup_dir,
        )
    if not args.skip_qdrant:
        report.qdrant = await migrate_qdrant(
            old=old,
            new=new,
            mappings=mappings,
            apply=args.apply,
            include_text=args.include_text,
            limit=args.qdrant_limit,
        )
    return report


def main() -> int:
    from app.services.system_data_root import get_system_data_root

    data_root = get_system_data_root(create=False)
    parser = argparse.ArgumentParser(description="Rename a project identity in MnemoForge storage.")
    parser.add_argument("--old", default="supermemory", help="Old project identifier.")
    parser.add_argument("--new", default="mnemoforge", help="New project identifier.")
    parser.add_argument("--data-dir", default=str(data_root), help="Directory containing SQLite stores.")
    parser.add_argument("--backup-dir", default=str(data_root / "identity_migration_backups"))
    parser.add_argument("--qdrant-limit", type=int, default=0, help="Maximum Qdrant points to scan; 0 means all.")
    parser.add_argument("--skip-qdrant", action="store_true", help="Only inspect/update SQLite files.")
    parser.add_argument("--include-text", action="store_true", help="Also rewrite free-text history fields.")
    parser.add_argument("--ensure-alias", action="store_true", help="Create alias mapping old -> new before migration.")
    parser.add_argument("--alias-reason", default="", help="Reason stored with --ensure-alias.")
    parser.add_argument("--mapping-file", default="", help="JSON mapping file for batch canonicalization.")
    parser.add_argument("--use-alias-table", action="store_true", help="Use active mappings from project_identity.db.")
    parser.add_argument("--apply", action="store_true", help="Apply changes. Default is dry-run.")
    args = parser.parse_args()

    report = asyncio.run(run(args))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
