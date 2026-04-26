from pathlib import Path
from uuid import uuid4

import pytest

from app.config import settings


def _workspace_tmp_dir() -> Path:
    path = Path.cwd() / ".tmp" / f"ingest-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


@pytest.mark.asyncio
async def test_ingest_file_resolves_relative_path_from_request_cwd(client):
    tmp_dir = _workspace_tmp_dir()
    try:
        notes = tmp_dir / "notes.md"
        notes.write_text("# Notes\n\nThis paragraph is long enough to be stored in memory.\n", encoding="utf-8")

        response = await client.post(
            "/api/v1/ingest/file",
            json={
                "path": "notes.md",
                "cwd": str(tmp_dir),
                "agent_id": "tester",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "inserted": 1,
            "failed": 0,
            "skipped": 0,
            "files_processed": 1,
        }
    finally:
        notes.unlink(missing_ok=True)
        tmp_dir.rmdir()


@pytest.mark.asyncio
async def test_ingest_dir_resolves_relative_path_from_allowed_root(client, monkeypatch):
    tmp_dir = _workspace_tmp_dir()
    docs_dir = tmp_dir / "docs"
    docs_dir.mkdir()
    guide = docs_dir / "guide.md"
    guide.write_text(
        "# Guide\n\nThis paragraph is long enough to be stored in memory.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "ingest_allowed_roots", str(tmp_dir))

    try:
        response = await client.post(
            "/api/v1/ingest/dir",
            json={
                "path": "docs",
                "agent_id": "tester",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "inserted": 1,
            "failed": 0,
            "skipped": 0,
            "files_processed": 1,
        }
    finally:
        guide.unlink(missing_ok=True)
        docs_dir.rmdir()
        tmp_dir.rmdir()


@pytest.mark.asyncio
async def test_ingest_file_returns_403_not_false_404_when_existing_cwd_path_is_outside_allowed_roots(client, monkeypatch):
    allowed_root = _workspace_tmp_dir()
    external_root = _workspace_tmp_dir()
    notes = external_root / "notes.md"
    notes.write_text("# Notes\n\nExisting file outside allowed roots.\n", encoding="utf-8")

    monkeypatch.setattr(settings, "ingest_allowed_roots", str(allowed_root))
    try:
        response = await client.post(
            "/api/v1/ingest/file",
            json={
                "path": "notes.md",
                "cwd": str(external_root),
                "agent_id": "tester",
            },
        )

        assert response.status_code == 403
        assert "outside allowed roots" in response.json()["detail"]
    finally:
        notes.unlink(missing_ok=True)
        external_root.rmdir()
        allowed_root.rmdir()
