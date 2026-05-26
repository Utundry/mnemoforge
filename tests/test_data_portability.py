from __future__ import annotations

import sqlite3

import pytest

import app.routers.admin as admin_router
from app.services.data_portability_service import (
    PORTABLE_EXPORT_FORMAT_VERSION,
    build_portable_export_package,
    build_portable_export_plan,
)


def _create_sample_store(root):
    db_path = root / "sample.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                project TEXT,
                content TEXT,
                payload BLOB
            )
            """
        )
        conn.execute(
            "INSERT INTO memories(id, project, content, payload) VALUES (?, ?, ?, ?)",
            ("m1", "mnemoforge", "portable memory", b"\x00\x01"),
        )
        conn.execute(
            "INSERT INTO memories(id, project, content, payload) VALUES (?, ?, ?, ?)",
            ("m2", "ui_avt", "other project memory", b"\x02"),
        )
        conn.execute("CREATE TABLE global_settings (name TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO global_settings(name, value) VALUES ('schema', 'v1')")
        conn.commit()
    return db_path


def test_portable_export_plan_counts_project_scoped_rows(tmp_path):
    _create_sample_store(tmp_path)

    plan = build_portable_export_plan(root=tmp_path, project="mnemoforge", row_limit_per_table=10)

    assert plan["format_version"] == PORTABLE_EXPORT_FORMAT_VERSION
    assert plan["summary"]["store_count"] == 1
    store = plan["stores"][0]
    memories = next(table for table in store["tables"] if table["name"] == "memories")
    settings = next(table for table in store["tables"] if table["name"] == "global_settings")
    assert memories["row_count"] == 2
    assert memories["matching_count"] == 1
    assert memories["exported_count"] == 1
    assert memories["project_filter_applied"] is True
    assert settings["matching_count"] == 1
    assert settings["project_filter_applied"] is False


def test_portable_export_package_serializes_rows_and_blobs(tmp_path):
    _create_sample_store(tmp_path)

    package = build_portable_export_package(root=tmp_path, project="mnemoforge", row_limit_per_table=10)

    assert package["manifest"]["format_version"] == PORTABLE_EXPORT_FORMAT_VERSION
    assert package["manifest"]["restore_policy"]["qdrant"] == "rebuild-from-sqlite"
    memories = next(table for table in package["stores"][0]["tables"] if table["name"] == "memories")
    assert len(memories["rows"]) == 1
    assert memories["rows"][0]["id"] == "m1"
    assert memories["rows"][0]["payload"] == {"__encoding": "base64", "data": "AAE="}


def test_portable_export_package_can_omit_rows(tmp_path):
    _create_sample_store(tmp_path)

    package = build_portable_export_package(root=tmp_path, project="mnemoforge", include_rows=False)

    table = package["stores"][0]["tables"][0]
    assert "rows" not in table
    assert package["manifest"]["summary"]["exported_rows"] >= 1


def test_portable_export_excludes_test_stores_by_default(tmp_path):
    _create_sample_store(tmp_path)
    test_dir = tmp_path / "test_mcp_checkpoint_drafts"
    test_dir.mkdir()
    _create_sample_store(test_dir)

    plan = build_portable_export_plan(root=tmp_path)
    diagnostic_plan = build_portable_export_plan(root=tmp_path, include_test_stores=True)

    assert [store["path"] for store in plan["stores"]] == ["sample.db"]
    assert sorted(store["path"] for store in diagnostic_plan["stores"]) == [
        "sample.db",
        "test_mcp_checkpoint_drafts/sample.db",
    ]


@pytest.mark.asyncio
async def test_admin_data_portability_plan_endpoint(client, monkeypatch):
    def fake_plan(**kwargs):
        return {
            "format_version": PORTABLE_EXPORT_FORMAT_VERSION,
            "project": kwargs.get("project") or "",
            "row_limit_per_table": kwargs.get("row_limit_per_table"),
            "stores": [],
            "summary": {"store_count": 0, "table_count": 0, "exported_rows": 0, "truncated_tables": 0},
        }

    monkeypatch.setattr(admin_router, "build_portable_export_plan", fake_plan)

    resp = await client.get("/api/v1/admin/data-portability/export/plan?project=mnemoforge&row_limit_per_table=7")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project"] == "mnemoforge"
    assert data["row_limit_per_table"] == 7


@pytest.mark.asyncio
async def test_admin_data_portability_export_endpoint(client, monkeypatch):
    def fake_export(**kwargs):
        return {
            "manifest": {
                "format_version": PORTABLE_EXPORT_FORMAT_VERSION,
                "project": kwargs.get("project") or "",
                "row_limit_per_table": kwargs.get("row_limit_per_table"),
                "summary": {"store_count": 0, "table_count": 0, "exported_rows": 0, "truncated_tables": 0},
                "restore_policy": {"mode": "preview-required"},
            },
            "stores": [],
        }

    monkeypatch.setattr(admin_router, "build_portable_export_package", fake_export)

    resp = await client.get("/api/v1/admin/data-portability/export?project=mnemoforge&row_limit_per_table=9")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["manifest"]["project"] == "mnemoforge"
    assert data["manifest"]["row_limit_per_table"] == 9
