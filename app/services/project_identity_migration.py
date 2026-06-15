from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any


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
            rows = conn.execute(f"SELECT {', '.join(selected_columns)} FROM {table}").fetchall()
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
