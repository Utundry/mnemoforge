import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrate_project_identity import migrate_sqlite_file, migrate_value, migrate_value_with_mappings, run


def test_migrate_value_rewrites_structured_refs_without_free_text():
    raw = '{"project": "mnemoforge", "tags": ["project:supermemory"], "content": "SuperMemory was the old name."}'

    migrated = migrate_value(raw, "supermemory", "mnemoforge", include_text=False)

    assert '"project": "mnemoforge"' in migrated
    assert '"project:mnemoforge"' in migrated
    assert "SuperMemory was the old name." in migrated


def test_migrate_value_can_rewrite_free_text_when_requested():
    assert (
        migrate_value("SuperMemory was the old name.", "SuperMemory", "MnemoForge", include_text=True)
        == "MnemoForge was the old name."
    )


def test_migrate_value_supports_batch_canonicalization():
    raw = '{"project": "supermemory", "tags": ["project:oldforge", "artifact:supermemory:x"]}'

    migrated = migrate_value_with_mappings(
        raw,
        [("supermemory", "mnemoforge"), ("oldforge", "mnemoforge")],
        include_text=False,
    )

    assert '"project": "mnemoforge"' in migrated
    assert '"project:mnemoforge"' in migrated
    assert "artifact:mnemoforge:x" in migrated


def test_sqlite_migration_updates_project_tags_and_artifact_keys(tmp_path: Path):
    db_path = tmp_path / "project_tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE project_tasks (id TEXT PRIMARY KEY, project TEXT, tags TEXT, linked_artifact_key TEXT, description TEXT)"
    )
    conn.execute(
        "INSERT INTO project_tasks VALUES (?, ?, ?, ?, ?)",
        (
            "row-1",
            "supermemory",
            '["project:supermemory", "task_status:active"]',
            "improvement:supermemory:abc",
            "Free text mentions SuperMemory and should stay historical.",
        ),
    )
    conn.commit()
    conn.close()

    report = migrate_sqlite_file(
        db_path,
        old="supermemory",
        new="mnemoforge",
        apply=True,
        include_text=False,
        backup_dir=tmp_path / "backups",
    )

    assert report["changed_rows"] == 1
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT project, tags, linked_artifact_key, description FROM project_tasks").fetchone()
    conn.close()
    assert row[0] == "mnemoforge"
    assert "project:mnemoforge" in row[1]
    assert row[2] == "improvement:mnemoforge:abc"
    assert "SuperMemory" in row[3]


@pytest.mark.asyncio
async def test_ensure_alias_is_dry_run_safe(tmp_path: Path):
    args = SimpleNamespace(
        old="supermemory",
        new="mnemoforge",
        data_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backups"),
        qdrant_limit=0,
        skip_qdrant=True,
        include_text=False,
        apply=False,
        ensure_alias=True,
        alias_reason="old working name",
        mapping_file="",
        use_alias_table=False,
    )

    report = await run(args)

    assert not (tmp_path / "project_identity.db").exists()
    assert report.sqlite_files["project_identity.db"]["dry_run_aliases"][1]["alias"] == "supermemory"
