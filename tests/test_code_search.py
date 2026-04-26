from __future__ import annotations

import shutil
from pathlib import Path

from app.services.memory_store import get_memory_store


def _workspace_tmp(name: str) -> Path:
    root = Path("tmp_test_code_search") / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


async def test_index_codebase_and_search_by_symbol(client):
    project_dir = _workspace_tmp("sample_project")
    (project_dir / "service.py").write_text(
        "class ReviewQueue:\n"
        "    def approve(self):\n"
        "        return 'approved'\n\n"
        "def build_skill_pack():\n"
        "    return ['adaptive']\n",
        encoding="utf-8",
    )

    index = await client.post("/api/v1/code/index", json={
        "path": str(project_dir),
        "agent_id": "code-test",
        "extensions": ["py"],
    })
    assert index.status_code == 200
    body = index.json()
    assert body["inserted"] >= 2
    assert body["files_processed"] == 1

    search = await client.post("/api/v1/code/search", json={
        "query": "ReviewQueue approve",
        "agent_id": "code-test",
        "limit": 5,
    })
    assert search.status_code == 200
    results = search.json()
    assert results["total"] >= 1
    assert results["hits"][0]["symbol"] in {"ReviewQueue", "approve"}
    assert results["hits"][0]["path"] == "service.py"


async def test_code_search_filters_by_path_prefix_and_language(client):
    root = _workspace_tmp("repo")
    app_dir = root / "app"
    tests_dir = root / "tests"
    app_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)

    (app_dir / "router.py").write_text(
        "def search_memories():\n"
        "    return 'router'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_router.py").write_text(
        "def test_search_memories():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    index = await client.post("/api/v1/code/index", json={
        "path": str(root),
        "agent_id": "code-test-filter",
        "extensions": ["py"],
    })
    assert index.status_code == 200

    search = await client.post("/api/v1/code/search", json={
        "query": "search_memories",
        "agent_id": "code-test-filter",
        "path_prefix": "app/",
        "language": "python",
        "limit": 5,
    })
    assert search.status_code == 200
    results = search.json()["hits"]
    assert len(results) >= 1
    assert all(hit["path"].startswith("app/") for hit in results)
    assert all(hit["language"] == "python" for hit in results)


async def test_code_index_persists_rebuild_metadata_in_sqlite(client):
    project_dir = _workspace_tmp("code_meta")
    (project_dir / "worker.py").write_text(
        "from pathlib import Path\n\n"
        "def rebuild_index() -> Path:\n"
        "    return Path('ok')\n",
        encoding="utf-8",
    )

    index = await client.post("/api/v1/code/index", json={
        "path": str(project_dir),
        "agent_id": "code-meta-test",
        "extensions": ["py"],
    })
    assert index.status_code == 200, index.text
    assert index.json()["inserted"] >= 1

    rows = await get_memory_store().list_by_category("code_component", limit=10)
    assert rows
    row = rows[0]
    meta = row["metadata"]
    assert meta["category"] == "code_component"
    assert meta["agent_id"] == "code-meta-test"
    assert meta["memory_type"] == "context"
    assert meta["source"].startswith("code-index:")
    assert "language:python" in meta["tags"]
    assert meta["code_path"] == "worker.py"
