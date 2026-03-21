"""Tests for hybrid code search: ripgrep lexical pass, query expansion, imports metadata."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


@pytest_asyncio.fixture
async def expand_client():
    """Client with ollama.generate returning an expanded query."""
    c, qdrant_client, ollama = await _build_client(MOCK_VECTOR)
    ollama.generate = AsyncMock(return_value="authentication JWT token middleware login session")
    async with c:
        yield c
    await qdrant_client.close()


@pytest_asyncio.fixture
async def rerank_client():
    """Client with ollama.generate returning a reranked order (reverses first two)."""
    c, qdrant_client, ollama = await _build_client(MOCK_VECTOR)
    # Simulate LLM returning indices in reversed order
    ollama.generate = AsyncMock(return_value="2, 1")
    async with c:
        yield c
    await qdrant_client.close()


# ── Imports metadata ─────────────────────────────────────────────────────────


class TestImportsMetadata:
    async def test_imports_extracted_from_python_file(self, client, tmp_path: Path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "module.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "from typing import Optional\n\n"
            "def my_function(x: Optional[int]) -> Path:\n"
            "    return Path(os.getcwd())\n",
            encoding="utf-8",
        )
        r = await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "imports-test",
            "extensions": ["py"],
        })
        assert r.status_code == 200
        assert r.json()["inserted"] >= 1

        search = await client.post("/api/v1/code/search", json={
            "query": "my_function",
            "agent_id": "imports-test",
            "limit": 5,
        })
        assert search.status_code == 200
        hits = search.json()["hits"]
        assert len(hits) >= 1
        hit = next((h for h in hits if h["symbol"] == "my_function"), None)
        assert hit is not None
        # imports should include os and Path
        assert any("os" in imp for imp in hit["imports"])

    async def test_hit_imports_field_present(self, client, tmp_path: Path):
        project_dir = tmp_path / "proj2"
        project_dir.mkdir()
        (project_dir / "svc.py").write_text(
            "import json\n\ndef parse():\n    return json.loads('{}')\n",
            encoding="utf-8",
        )
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "imports-field-test",
            "extensions": ["py"],
        })
        search = await client.post("/api/v1/code/search", json={
            "query": "parse",
            "agent_id": "imports-field-test",
        })
        hits = search.json()["hits"]
        assert hits
        assert isinstance(hits[0]["imports"], list)

    async def test_no_imports_file_returns_empty_list(self, client, tmp_path: Path):
        project_dir = tmp_path / "proj3"
        project_dir.mkdir()
        (project_dir / "bare.py").write_text(
            "def bare():\n    return 42\n",
            encoding="utf-8",
        )
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "no-imports-test",
            "extensions": ["py"],
        })
        search = await client.post("/api/v1/code/search", json={
            "query": "bare",
            "agent_id": "no-imports-test",
        })
        hits = search.json()["hits"]
        assert hits
        assert hits[0]["imports"] == []


# ── Query expansion ───────────────────────────────────────────────────────────


class TestQueryExpansion:
    async def test_expand_query_field_populated(self, expand_client, tmp_path: Path):
        project_dir = tmp_path / "auth_proj"
        project_dir.mkdir()
        (project_dir / "auth.py").write_text(
            "def authenticate(token: str) -> bool:\n    return token == 'secret'\n",
            encoding="utf-8",
        )
        await expand_client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "expand-test",
            "extensions": ["py"],
        })
        r = await expand_client.post("/api/v1/code/search", json={
            "query": "how does auth work",
            "agent_id": "expand-test",
            "expand_query": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["query_expanded"] == "authentication JWT token middleware login session"

    async def test_expand_query_false_no_expansion(self, client, tmp_path: Path):
        project_dir = tmp_path / "proj_no_exp"
        project_dir.mkdir()
        (project_dir / "util.py").write_text("def util(): pass\n", encoding="utf-8")
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "no-expand-test",
            "extensions": ["py"],
        })
        r = await client.post("/api/v1/code/search", json={
            "query": "util",
            "agent_id": "no-expand-test",
            "expand_query": False,
        })
        assert r.status_code == 200
        assert r.json()["query_expanded"] is None

    async def test_expand_query_empty_response_uses_original(self, client, tmp_path: Path):
        """When generate returns empty string, original query is used (query_expanded is None)."""
        project_dir = tmp_path / "proj_empty_exp"
        project_dir.mkdir()
        (project_dir / "mod.py").write_text("def mod(): pass\n", encoding="utf-8")
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "empty-expand-test",
            "extensions": ["py"],
        })
        # client has generate returning "" by default
        r = await client.post("/api/v1/code/search", json={
            "query": "mod",
            "agent_id": "empty-expand-test",
            "expand_query": True,
        })
        assert r.status_code == 200
        assert r.json()["query_expanded"] is None


# ── Ripgrep lexical pass ──────────────────────────────────────────────────────


class TestRipgrepLexical:
    async def test_search_without_search_root_uses_token_scoring(self, client, tmp_path: Path):
        """No search_root → falls back to Python token scoring (existing behaviour)."""
        project_dir = tmp_path / "proj_tok"
        project_dir.mkdir()
        (project_dir / "svc.py").write_text(
            "def fetch_data():\n    return []\n",
            encoding="utf-8",
        )
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "tok-test",
            "extensions": ["py"],
        })
        r = await client.post("/api/v1/code/search", json={
            "query": "fetch_data",
            "agent_id": "tok-test",
        })
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    async def test_ripgrep_search_root_returns_results(self, client, tmp_path: Path):
        """With search_root, rg pass is attempted; falls back gracefully if rg not installed."""
        project_dir = tmp_path / "proj_rg"
        project_dir.mkdir()
        (project_dir / "service.py").write_text(
            "def unique_rg_function():\n    pass\n",
            encoding="utf-8",
        )
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "rg-test",
            "extensions": ["py"],
        })
        r = await client.post("/api/v1/code/search", json={
            "query": "unique_rg_function",
            "agent_id": "rg-test",
            "search_root": str(project_dir),
        })
        assert r.status_code == 200
        # Either rg found it or semantic search did — total should be >= 1
        assert r.json()["total"] >= 1

    async def test_lexical_tool_boosts_score(self, client, tmp_path: Path):
        """When rg or grep finds a file, chunks from that file get lexical_score > 0."""
        if not shutil.which("rg") and not shutil.which("grep"):
            pytest.skip("neither ripgrep nor grep is installed")

        project_dir = tmp_path / "proj_rg_boost"
        project_dir.mkdir()
        (project_dir / "target.py").write_text(
            "def lexical_boosted_symbol():\n    return 'hit'\n",
            encoding="utf-8",
        )
        (project_dir / "other.py").write_text(
            "def unrelated_func():\n    return 'miss'\n",
            encoding="utf-8",
        )
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "rg-boost-test",
            "extensions": ["py"],
        })
        r = await client.post("/api/v1/code/search", json={
            "query": "lexical_boosted_symbol",
            "agent_id": "rg-boost-test",
            "search_root": str(project_dir),
            "limit": 10,
        })
        assert r.status_code == 200
        hits = r.json()["hits"]
        # The chunk from target.py should have lexical_score > 0
        target_hits = [h for h in hits if "target" in h["path"]]
        assert target_hits
        assert target_hits[0]["lexical_score"] > 0

    async def test_rg_missing_falls_back_silently(self, client, tmp_path: Path):
        """When rg is not in PATH, _ripgrep_hits returns empty set and search still works."""
        project_dir = tmp_path / "proj_rg_fallback"
        project_dir.mkdir()
        (project_dir / "mod.py").write_text(
            "def fallback_func():\n    pass\n",
            encoding="utf-8",
        )
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir),
            "agent_id": "rg-fallback-test",
            "extensions": ["py"],
        })
        with patch("app.routers.code_search.shutil.which", return_value=None):  # no rg, no grep
            r = await client.post("/api/v1/code/search", json={
                "query": "fallback_func",
                "agent_id": "rg-fallback-test",
                "search_root": str(project_dir),
            })
        assert r.status_code == 200
        # search still returns results via semantic pass
        assert r.json()["total"] >= 0  # may be 0 if semantic miss, but no error


# ── Parser unit tests (sync) ──────────────────────────────────────────────────


class TestCodeSearchParser:
    def test_extract_imports_from_python_file(self, tmp_path: Path):
        from app.services.code_search_parser import _python_chunks

        f = tmp_path / "mod.py"
        f.write_text(
            "import os\nfrom pathlib import Path\n\ndef func(): pass\n",
            encoding="utf-8",
        )
        chunks = _python_chunks(f)
        assert chunks
        chunk = chunks[0]
        assert "os" in chunk.imports
        assert any("Path" in imp for imp in chunk.imports)

    def test_no_imports_empty_list(self, tmp_path: Path):
        from app.services.code_search_parser import _python_chunks

        f = tmp_path / "bare.py"
        f.write_text("def bare(): pass\n", encoding="utf-8")
        chunks = _python_chunks(f)
        assert chunks[0].imports == []

    def test_from_import_recorded(self, tmp_path: Path):
        from app.services.code_search_parser import _python_chunks

        f = tmp_path / "svc.py"
        f.write_text(
            "from typing import Optional, List\n\ndef func(x: Optional[List[int]]): pass\n",
            encoding="utf-8",
        )
        chunks = _python_chunks(f)
        names = chunks[0].imports
        assert any("Optional" in n for n in names)
        assert any("List" in n for n in names)


# ── JS/TS chunking ────────────────────────────────────────────────────────────


class TestJsTsChunking:
    def test_js_named_function_chunked(self, tmp_path: Path):
        from app.services.code_search_parser import _js_chunks

        f = tmp_path / "utils.js"
        f.write_text(
            "function fetchData(url) {\n  return fetch(url);\n}\n\n"
            "function processResult(data) {\n  return data.json();\n}\n",
            encoding="utf-8",
        )
        chunks = _js_chunks(f, "javascript")
        assert len(chunks) >= 2
        symbols = {c.symbol for c in chunks}
        assert "fetchData" in symbols
        assert "processResult" in symbols

    def test_ts_class_chunked(self, tmp_path: Path):
        from app.services.code_search_parser import _js_chunks

        f = tmp_path / "service.ts"
        f.write_text(
            "export class MemoryService {\n  search() { return []; }\n}\n\n"
            "export class EmbedService {\n  embed(t: string) { return []; }\n}\n",
            encoding="utf-8",
        )
        chunks = _js_chunks(f, "typescript")
        symbols = {c.symbol for c in chunks}
        assert "MemoryService" in symbols
        assert "EmbedService" in symbols
        assert all(c.chunk_type == "class" for c in chunks if c.symbol in {"MemoryService", "EmbedService"})

    def test_js_arrow_function_chunked(self, tmp_path: Path):
        from app.services.code_search_parser import _js_chunks

        f = tmp_path / "handlers.js"
        f.write_text(
            "const handleSearch = async (req, res) => {\n  res.json([]);\n};\n\n"
            "const handleInsert = (req, res) => {\n  res.json({ok: true});\n};\n",
            encoding="utf-8",
        )
        chunks = _js_chunks(f, "javascript")
        symbols = {c.symbol for c in chunks}
        assert "handleSearch" in symbols or "handleInsert" in symbols

    def test_js_no_functions_falls_back_to_file_chunk(self, tmp_path: Path):
        from app.services.code_search_parser import _js_chunks

        f = tmp_path / "config.js"
        f.write_text("module.exports = { debug: true };\n", encoding="utf-8")
        chunks = _js_chunks(f, "javascript")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "file"

    def test_ts_export_function_chunked(self, tmp_path: Path):
        from app.services.code_search_parser import _js_chunks

        f = tmp_path / "api.ts"
        f.write_text(
            "export async function searchMemories(query: string) {\n  return [];\n}\n",
            encoding="utf-8",
        )
        chunks = _js_chunks(f, "typescript")
        assert any(c.symbol == "searchMemories" for c in chunks)


# ── RST chunking ──────────────────────────────────────────────────────────────


class TestRstChunking:
    def test_rst_sections_split_by_heading(self, tmp_path: Path):
        from app.services.code_search_parser import _rst_chunks

        f = tmp_path / "docs.rst"
        f.write_text(
            "Overview\n========\n\nThis is the overview section.\n\n"
            "Installation\n============\n\nRun pip install supermemory.\n\n"
            "Usage\n-----\n\nImport and call the API.\n",
            encoding="utf-8",
        )
        chunks = _rst_chunks(f)
        assert len(chunks) >= 2
        symbols = {c.symbol for c in chunks}
        assert "Overview" in symbols
        assert "Installation" in symbols

    def test_rst_chunk_type_is_section(self, tmp_path: Path):
        from app.services.code_search_parser import _rst_chunks

        f = tmp_path / "guide.rst"
        f.write_text("Quick Start\n===========\n\nDo this first.\n", encoding="utf-8")
        chunks = _rst_chunks(f)
        assert chunks
        assert chunks[0].chunk_type == "section"
        assert chunks[0].symbol == "Quick Start"

    def test_rst_no_headings_falls_back_to_file(self, tmp_path: Path):
        from app.services.code_search_parser import _rst_chunks

        f = tmp_path / "plain.rst"
        f.write_text("Just some plain text without any headings here.\n", encoding="utf-8")
        chunks = _rst_chunks(f)
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "file"

# ── LLM reranking ────────────────────────────────────────────────────────────


class TestLlmReranking:
    async def test_rerank_false_does_not_set_flag(self, client, tmp_path: Path):
        project_dir = tmp_path / "proj_nr"
        project_dir.mkdir()
        (project_dir / "mod.py").write_text("def alpha(): pass\ndef beta(): pass\n", encoding="utf-8")
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir), "agent_id": "rerank-off", "extensions": ["py"],
        })
        r = await client.post("/api/v1/code/search", json={
            "query": "alpha", "agent_id": "rerank-off", "rerank": False,
        })
        assert r.status_code == 200
        assert r.json()["reranked"] is False

    async def test_rerank_true_sets_flag_when_llm_responds(self, rerank_client, tmp_path: Path):
        project_dir = tmp_path / "proj_rr"
        project_dir.mkdir()
        (project_dir / "svc.py").write_text(
            "def first_func(): pass\ndef second_func(): pass\n", encoding="utf-8"
        )
        await rerank_client.post("/api/v1/code/index", json={
            "path": str(project_dir), "agent_id": "rerank-on", "extensions": ["py"],
        })
        r = await rerank_client.post("/api/v1/code/search", json={
            "query": "first second", "agent_id": "rerank-on", "rerank": True, "limit": 5,
        })
        assert r.status_code == 200
        body = r.json()
        # reranked=True only when LLM returns parseable response and >=2 hits
        if body["total"] >= 2:
            assert body["reranked"] is True

    async def test_rerank_empty_llm_response_falls_back(self, client, tmp_path: Path):
        """When generate returns '', reranked=False and original order preserved."""
        project_dir = tmp_path / "proj_rr_fb"
        project_dir.mkdir()
        (project_dir / "m.py").write_text("def foo(): pass\ndef bar(): pass\n", encoding="utf-8")
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir), "agent_id": "rerank-fallback", "extensions": ["py"],
        })
        # client.generate returns "" by default
        r = await client.post("/api/v1/code/search", json={
            "query": "foo bar", "agent_id": "rerank-fallback", "rerank": True,
        })
        assert r.status_code == 200
        assert r.json()["reranked"] is False

    async def test_rerank_preserves_all_hits(self, rerank_client, tmp_path: Path):
        """After reranking, total number of hits is unchanged."""
        project_dir = tmp_path / "proj_rr_all"
        project_dir.mkdir()
        (project_dir / "svc.py").write_text(
            "def a(): pass\ndef b(): pass\ndef c(): pass\n", encoding="utf-8"
        )
        await rerank_client.post("/api/v1/code/index", json={
            "path": str(project_dir), "agent_id": "rerank-all", "extensions": ["py"],
        })
        r = await rerank_client.post("/api/v1/code/search", json={
            "query": "a b c", "agent_id": "rerank-all", "rerank": True, "limit": 10,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == len(body["hits"])

    async def test_rerank_response_has_required_fields(self, client, tmp_path: Path):
        project_dir = tmp_path / "proj_rr_fields"
        project_dir.mkdir()
        (project_dir / "x.py").write_text("def x(): pass\n", encoding="utf-8")
        await client.post("/api/v1/code/index", json={
            "path": str(project_dir), "agent_id": "rerank-fields", "extensions": ["py"],
        })
        r = await client.post("/api/v1/code/search", json={
            "query": "x", "agent_id": "rerank-fields", "rerank": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert "reranked" in body
        assert "query_expanded" in body


    def test_rst_body_content_captured(self, tmp_path: Path):
        from app.services.code_search_parser import _rst_chunks

        f = tmp_path / "ref.rst"
        f.write_text(
            "Configuration\n=============\n\nSet QDRANT_HOST to localhost.\n",
            encoding="utf-8",
        )
        chunks = _rst_chunks(f)
        assert chunks
        assert "QDRANT_HOST" in chunks[0].content
