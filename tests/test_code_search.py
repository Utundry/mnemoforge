from __future__ import annotations

from pathlib import Path


async def test_index_codebase_and_search_by_symbol(client, tmp_path: Path):
    project_dir = tmp_path / "sample_project"
    project_dir.mkdir()
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


async def test_code_search_filters_by_path_prefix_and_language(client, tmp_path: Path):
    root = tmp_path / "repo"
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
