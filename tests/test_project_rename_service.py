import sqlite3
from pathlib import Path

import pytest

from app.services.project_rename_service import rename_project_identity


def _seed_project_tasks(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE project_tasks (id TEXT PRIMARY KEY, project TEXT, tags TEXT, artifact_key TEXT, description TEXT)"
    )
    conn.execute(
        "INSERT INTO project_tasks VALUES (?, ?, ?, ?, ?)",
        (
            "task-1",
            "supermemory",
            '["project:supermemory", "status:open"]',
            "task:supermemory:release",
            "Historical text mentioning SuperMemory stays intact by default.",
        ),
    )
    conn.commit()
    conn.close()


def test_project_rename_dry_run_does_not_create_alias_store(tmp_path: Path):
    _seed_project_tasks(tmp_path / "project_tasks.db")

    report = rename_project_identity(
        old_project_id="supermemory",
        new_project_id="mnemoforge",
        apply=False,
        data_dir=tmp_path,
        backup_dir=tmp_path / "backups",
    )

    assert report["mode"] == "dry-run"
    assert report["summary"]["changed_rows"] == 3
    assert not (tmp_path / "project_identity.db").exists()
    assert report["sqlite_files"]["project_identity.db"]["dry_run_aliases"][1]["alias"] == "supermemory"

    conn = sqlite3.connect(str(tmp_path / "project_tasks.db"))
    row = conn.execute("SELECT project, tags, artifact_key, description FROM project_tasks").fetchone()
    conn.close()
    assert row[0] == "supermemory"
    assert "project:supermemory" in row[1]
    assert row[2] == "task:supermemory:release"
    assert "SuperMemory" in row[3]


def test_project_rename_apply_updates_structured_refs_and_aliases(tmp_path: Path):
    _seed_project_tasks(tmp_path / "project_tasks.db")

    report = rename_project_identity(
        old_project_id="supermemory",
        new_project_id="mnemoforge",
        apply=True,
        reason="release rename",
        data_dir=tmp_path,
        backup_dir=tmp_path / "backups",
    )

    assert report["mode"] == "apply"
    assert report["summary"]["changed_rows"] == 3
    assert report["sqlite_files"]["project_identity.db"]["aliases"][1]["project_id"] == "mnemoforge"
    assert report["summary"]["backups_created"] == 1

    conn = sqlite3.connect(str(tmp_path / "project_tasks.db"))
    row = conn.execute("SELECT project, tags, artifact_key, description FROM project_tasks").fetchone()
    alias_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='project_identity_aliases'"
    ).fetchone()
    conn.close()
    assert row[0] == "mnemoforge"
    assert "project:mnemoforge" in row[1]
    assert row[2] == "task:mnemoforge:release"
    assert "SuperMemory" in row[3]
    assert alias_row is None

    alias_conn = sqlite3.connect(str(tmp_path / "project_identity.db"))
    aliases = alias_conn.execute(
        "SELECT alias, project_id, reason FROM project_identity_aliases ORDER BY alias"
    ).fetchall()
    alias_conn.close()
    assert ("supermemory", "mnemoforge", "canonical project id") in aliases
    assert ("supermemory", "mnemoforge", "release rename") in aliases


def test_project_rename_rejects_noop(tmp_path: Path):
    with pytest.raises(ValueError):
        rename_project_identity(
            old_project_id="mnemoforge",
            new_project_id="mnemoforge",
            data_dir=tmp_path,
            backup_dir=tmp_path / "backups",
        )
