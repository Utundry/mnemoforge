from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import pytest

from app.models.docs import DocsSection, DocsStatus
from app.services import docs_service
from app.services.component_docs_store import get_component_docs_store


@pytest.mark.asyncio
async def test_rebuild_docs_preserves_effective_and_stages_candidate(monkeypatch):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-candidates-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    try:
        state = {"overview": "Overview v1", "architecture": "Architecture v1"}

        async def fake_fetch_skills(*args, **kwargs):
            return []

        async def fake_snapshot(*args, **kwargs):
            return {
                "project_id": "supermemory",
                "components": [],
                "laws": [],
                "improvements": [],
                "runtime_hints": [],
                "memoirs": [],
                "tasks": [],
            }

        async def fake_gen_overview(*args, **kwargs):
            return state["overview"]

        async def fake_gen_architecture(*args, **kwargs):
            return state["architecture"]

        monkeypatch.setattr(docs_service, "gather_project_knowledge_snapshot", fake_snapshot)
        monkeypatch.setattr(docs_service, "_fetch_skills", fake_fetch_skills)
        monkeypatch.setattr(docs_service, "_gen_overview", fake_gen_overview)
        monkeypatch.setattr(docs_service, "_gen_architecture", fake_gen_architecture)
        monkeypatch.setattr(docs_service, "_gen_features", lambda improvements: "Features")
        monkeypatch.setattr(docs_service, "_gen_pending", lambda improvements: "Pending")
        monkeypatch.setattr(docs_service, "_gen_runtime_hints", lambda runtime_hints: "Runtime hints")
        monkeypatch.setattr(docs_service, "_gen_tasks", lambda tasks: "Tasks")
        monkeypatch.setattr(docs_service, "_gen_decisions", lambda memoirs: "Decisions")
        monkeypatch.setattr(docs_service, "_gen_skills", lambda skills: "Skills")
        monkeypatch.setattr(docs_service, "_gen_performance", lambda project: "Performance")

        first = await docs_service.rebuild_docs("supermemory", object(), "ignored")
        assert first.sections["overview"].content == "Overview v1"
        assert first.candidate_sections == {}

        state["overview"] = "Overview v2"
        state["architecture"] = "Architecture v2"
        second = await docs_service.rebuild_docs("supermemory", object(), "ignored")
        assert second.sections["overview"].content == "Overview v1"
        assert second.candidate_sections["overview"].content == "Overview v2"
        assert second.candidate_generated_at is not None
        loaded = docs_service.load_docs_cache("supermemory")
        assert loaded is not None
        assert loaded.sections["overview"].content == "Overview v1"
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_rebuild_docs_skips_when_snapshot_commit_is_unchanged(monkeypatch):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-skip-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    try:
        calls = {"overview": 0, "architecture": 0}

        async def fake_fetch_skills(*args, **kwargs):
            return []

        async def fake_snapshot(*args, **kwargs):
            return {
                "project_id": "supermemory",
                "components": [{
                    "name": "Context",
                    "component_id": "context",
                    "snapshot": {
                        "source_mode": "git_snapshot",
                        "repo": "https://github.com/example/supermemory",
                        "branch": "main",
                        "commit_sha": "abc123def456",
                        "dirty_workspace": False,
                    },
                }],
                "laws": [],
                "improvements": [],
                "runtime_hints": [],
                "memoirs": [],
                "tasks": [],
            }

        async def fake_gen_overview(*args, **kwargs):
            calls["overview"] += 1
            return "Overview v1"

        async def fake_gen_architecture(*args, **kwargs):
            calls["architecture"] += 1
            return "Architecture v1"

        monkeypatch.setattr(docs_service, "gather_project_knowledge_snapshot", fake_snapshot)
        monkeypatch.setattr(docs_service, "_fetch_skills", fake_fetch_skills)
        monkeypatch.setattr(docs_service, "_gen_overview", fake_gen_overview)
        monkeypatch.setattr(docs_service, "_gen_architecture", fake_gen_architecture)
        monkeypatch.setattr(docs_service, "_gen_features", lambda improvements: "Features")
        monkeypatch.setattr(docs_service, "_gen_pending", lambda improvements: "Pending")
        monkeypatch.setattr(docs_service, "_gen_runtime_hints", lambda runtime_hints: "Runtime hints")
        monkeypatch.setattr(docs_service, "_gen_tasks", lambda tasks: "Tasks")
        monkeypatch.setattr(docs_service, "_gen_decisions", lambda memoirs: "Decisions")
        monkeypatch.setattr(docs_service, "_gen_skills", lambda skills: "Skills")
        monkeypatch.setattr(docs_service, "_gen_performance", lambda project: "Performance")

        first = await docs_service.rebuild_docs("supermemory", object(), "ignored", force=True)
        second = await docs_service.rebuild_docs("supermemory", object(), "ignored", force=False)

        assert first.snapshot["commit_sha"] == "abc123def456"
        assert first.last_rebuild_mode == "rebuild"
        assert second.generated_at == first.generated_at
        assert second.snapshot["commit_sha"] == "abc123def456"
        assert calls == {"overview": 1, "architecture": 1}
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_rebuild_docs_uses_cheap_diff_scoped_mode_for_narrow_component_changes(monkeypatch):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-diff-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    try:
        calls: list[tuple[str, bool]] = []
        state = {"commit_sha": "abc123def456"}

        async def fake_fetch_skills(*args, **kwargs):
            return []

        async def fake_snapshot(*args, **kwargs):
            return {
                "project_id": "supermemory",
                "components": [
                    {
                        "name": "Context",
                        "component_id": "context",
                        "purpose": "Context assembly",
                        "key_files": ["app/context.py"],
                        "snapshot": {
                            "source_mode": "git_snapshot",
                            "repo": "https://github.com/example/supermemory",
                            "branch": "main",
                            "commit_sha": state["commit_sha"],
                            "dirty_workspace": False,
                        },
                    },
                    {
                        "name": "Routing",
                        "component_id": "routing",
                        "purpose": "Routing layer",
                        "key_files": ["app/routing.py"],
                        "snapshot": {
                            "source_mode": "git_snapshot",
                            "repo": "https://github.com/example/supermemory",
                            "branch": "main",
                            "commit_sha": state["commit_sha"],
                            "dirty_workspace": False,
                        },
                    },
                ],
                "laws": [],
                "improvements": [],
                "runtime_hints": [],
                "memoirs": [],
                "tasks": [],
            }

        async def fake_gen_overview(*args, **kwargs):
            calls.append(("overview", bool(kwargs.get("use_cloud"))))
            return "Overview"

        async def fake_gen_architecture(*args, **kwargs):
            calls.append(("architecture", bool(kwargs.get("use_cloud"))))
            return "Architecture"

        monkeypatch.setattr(docs_service, "gather_project_knowledge_snapshot", fake_snapshot)
        monkeypatch.setattr(docs_service, "_fetch_skills", fake_fetch_skills)
        monkeypatch.setattr(docs_service, "_gen_overview", fake_gen_overview)
        monkeypatch.setattr(docs_service, "_gen_architecture", fake_gen_architecture)
        monkeypatch.setattr(docs_service, "_gen_features", lambda improvements: "Features")
        monkeypatch.setattr(docs_service, "_gen_pending", lambda improvements: "Pending")
        monkeypatch.setattr(docs_service, "_gen_runtime_hints", lambda runtime_hints: "Runtime hints")
        monkeypatch.setattr(docs_service, "_gen_tasks", lambda tasks: "Tasks")
        monkeypatch.setattr(docs_service, "_gen_decisions", lambda memoirs: "Decisions")
        monkeypatch.setattr(docs_service, "_gen_skills", lambda skills: "Skills")
        monkeypatch.setattr(docs_service, "_gen_performance", lambda project: "Performance")

        await docs_service.rebuild_docs("supermemory", object(), "ignored", force=True)
        state["commit_sha"] = "def789abc000"
        second = await docs_service.rebuild_docs(
            "supermemory",
            object(),
            "ignored",
            force=False,
            changed_component_ids=["context"],
        )

        assert ("overview", False) in calls
        assert ("architecture", False) in calls
        assert second.last_rebuild_mode == "diff_scoped"
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_docs_candidate_apply_and_discard_endpoints(client):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-candidates-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    try:
        status = DocsStatus(
            project="supermemory",
            generated_at=datetime.now(timezone.utc),
            sections={"overview": DocsSection(name="Overview", content="Effective overview.")},
            candidate_generated_at=datetime.now(timezone.utc),
            candidate_sections={"overview": DocsSection(name="Overview", content="Candidate overview.")},
        )
        docs_service._save_docs_cache("supermemory", status)

        candidate = await client.get("/api/v1/docs/status?project=supermemory&view=candidate")
        assert candidate.status_code == 200
        assert candidate.json()["sections"]["overview"]["content"] == "Candidate overview."

        applied = await client.post("/api/v1/docs/apply-candidate?project=supermemory")
        assert applied.status_code == 200
        assert applied.json()["sections"]["overview"]["content"] == "Candidate overview."
        assert applied.json()["candidate_sections"] == {}
        assert applied.json()["last_review_action"] == "apply_candidate"

        status = DocsStatus(
            project="supermemory",
            generated_at=datetime.now(timezone.utc),
            sections={"overview": DocsSection(name="Overview", content="Effective overview.")},
            candidate_generated_at=datetime.now(timezone.utc),
            candidate_sections={"overview": DocsSection(name="Overview", content="Discard me.")},
        )
        docs_service._save_docs_cache("supermemory", status)

        discarded = await client.post(
            "/api/v1/docs/discard-candidate?project=supermemory",
            json={"reviewed_by": "owner", "review_source": "dashboard_review", "reason": "Keep current docs"},
        )
        assert discarded.status_code == 200
        body = discarded.json()
        assert body["sections"]["overview"]["content"] == "Effective overview."
        assert body["candidate_sections"] == {}
        assert body["last_review_action"] == "discard_candidate"
        assert body["last_reviewed_by"] == "owner"
        assert body["last_review_source"] == "dashboard_review"
        assert body["last_review_reason"] == "Keep current docs"
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_docs_status_marks_projection_stale_when_component_snapshot_moves(client):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-stale-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    try:
        store = get_component_docs_store()
        await store.upsert_component(
            point_id="component-alpha",
            project_id="alpha",
            component_id="context",
            name="Context",
            purpose="Assemble project context.",
            implementation="Combines project knowledge surfaces.",
            snapshot={
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/supermemory",
                "branch": "main",
                "commit_sha": "new-commit-456",
                "dirty_workspace": False,
            },
        )

        status = DocsStatus(
            project="alpha",
            generated_at=datetime.now(timezone.utc),
            sections={"overview": DocsSection(name="Overview", content="Alpha overview.")},
            snapshot={
                "source_mode": "git_snapshot",
                "repo": "https://github.com/example/supermemory",
                "branch": "main",
                "commit_sha": "old-commit-123",
                "dirty_workspace": False,
            },
        )
        docs_service._save_docs_cache("alpha", status)

        loaded = docs_service.load_docs_cache("alpha")
        assert loaded is not None
        assert loaded.stale is True
        assert loaded.stale_reason == "component snapshot commit changed from old-commit-123 to new-commit-456"

        response = await client.get("/api/v1/docs/status?project=alpha")
        assert response.status_code == 200
        body = response.json()
        assert body["stale"] is True
        assert body["stale_reason"] == "component snapshot commit changed from old-commit-123 to new-commit-456"

        markdown = await client.get("/api/v1/docs/status.md?project=alpha")
        assert markdown.status_code == 200
        assert "_Projection is stale (component snapshot commit changed from old-commit-123 to new-commit-456)._" in markdown.text
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


def test_load_docs_cache_backfills_sqlite_store_from_legacy_file():
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-candidates-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    docs_service._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        status = DocsStatus(
            project="alpha",
            generated_at=datetime.now(timezone.utc),
            sections={"overview": DocsSection(name="Overview", content="Alpha overview.")},
        )
        docs_service._cache_path("alpha").write_text(status.model_dump_json(indent=2), encoding="utf-8")
        loaded = docs_service.load_docs_cache("alpha")
        assert loaded is not None
        assert loaded.sections["overview"].content == "Alpha overview."

        docs_service._cache_path("alpha").unlink()
        loaded_again = docs_service.load_docs_cache("alpha")
        assert loaded_again is not None
        assert loaded_again.sections["overview"].content == "Alpha overview."
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_docs_fetch_helpers_are_project_scoped():
    class FakePoint:
        def __init__(self, payload):
            self.payload = payload

    class FakeQdrant:
        async def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name="project_docs")])

        async def scroll(self, collection_name, scroll_filter=None, **kwargs):
            must = {cond.key: getattr(cond.match, "value", None) for cond in (scroll_filter.must if scroll_filter else [])}
            should = {
                cond.key: getattr(cond.match, "value", None)
                for cond in ((scroll_filter.should or []) if scroll_filter else [])
            }
            if collection_name == "memories" and must.get("category") == "improvement":
                assert must["project"] == "alpha"
                return [FakePoint({"project": "alpha", "title": "Alpha improvement"})], None
            if collection_name == "memories" and must.get("category") == "task_memoir":
                assert should["tags"] == "project:alpha"
                return [FakePoint({
                    "content": "Alpha memoir",
                    "timestamp": "2026-03-21T00:00:00+00:00",
                    "tags": ["project:alpha"],
                })], None
            if collection_name == "project_docs":
                assert must["project_id"] == "alpha"
                return [FakePoint({"project_id": "alpha", "component_id": "alpha-component"})], None
            raise AssertionError(f"Unexpected scroll call: {collection_name} {must}")

    fake = FakeQdrant()
    improvements = await docs_service._fetch_improvements(fake, "memories", "alpha")
    memoirs = await docs_service._fetch_memoirs(fake, "memories", "alpha")
    components = await docs_service._fetch_components(fake, "alpha")

    assert improvements[0]["project"] == "alpha"
    assert memoirs[0]["content"] == "Alpha memoir"
    assert components[0]["project_id"] == "alpha"


@pytest.mark.asyncio
async def test_fetch_skills_filters_global_skills_for_external_project(monkeypatch):
    class FakePoint:
        def __init__(self, payload):
            self.payload = payload

    class FakeQdrant:
        async def scroll(self, collection_name, scroll_filter=None, **kwargs):
            assert collection_name == "memories"
            return ([
                FakePoint({"skill_name": "global-skill", "tags": []}),
                FakePoint({"skill_name": "alpha-skill", "tags": ["project:alpha"]}),
                FakePoint({"skill_name": "pinned-global", "tags": [], "pinned": True}),
            ], None)

    monkeypatch.setattr(docs_service.settings, "self_project_id", "supermemory")
    skills = await docs_service._fetch_skills(FakeQdrant(), "memories", "alpha")
    names = {item.get("skill_name") for item in skills}
    assert "alpha-skill" in names
    assert "pinned-global" in names
    assert "global-skill" not in names


@pytest.mark.asyncio
async def test_fetch_skills_keeps_global_scope_for_self_project(monkeypatch):
    class FakePoint:
        def __init__(self, payload):
            self.payload = payload

    class FakeQdrant:
        async def scroll(self, collection_name, scroll_filter=None, **kwargs):
            assert collection_name == "memories"
            return ([
                FakePoint({"skill_name": "global-skill", "tags": []}),
                FakePoint({"skill_name": "another-global", "tags": []}),
            ], None)

    monkeypatch.setattr(docs_service.settings, "self_project_id", "supermemory")
    skills = await docs_service._fetch_skills(FakeQdrant(), "memories", "supermemory")
    names = {item.get("skill_name") for item in skills}
    assert names == {"global-skill", "another-global"}


@pytest.mark.asyncio
async def test_fetch_memoirs_accepts_project_field_without_project_tag():
    class FakePoint:
        def __init__(self, payload):
            self.payload = payload

    class FakeQdrant:
        async def scroll(self, collection_name, scroll_filter=None, **kwargs):
            assert collection_name == "memories"
            return ([
                FakePoint({
                    "project": "alpha",
                    "content": "Memoir with direct project field",
                    "timestamp": "2026-03-22T00:00:00+00:00",
                    "meta": {"quality_status": "grounded"},
                }),
                FakePoint({
                    "project": "beta",
                    "content": "Other project memoir",
                    "timestamp": "2026-03-23T00:00:00+00:00",
                    "meta": {"quality_status": "grounded"},
                }),
            ], None)

    memoirs = await docs_service._fetch_memoirs(FakeQdrant(), "memories", "alpha", limit=5)
    assert len(memoirs) == 1
    assert memoirs[0]["content"] == "Memoir with direct project field"


def test_gen_laws_formats_active_project_laws():
    rendered = docs_service._gen_laws([
        {
            "meta": {
                "title": "Require review",
                "statement": "Agents must review active laws before risky changes.",
                "rationale": "Avoids drift from confirmed project rules.",
            }
        }
    ])
    assert "Require review" in rendered
    assert "Agents must review active laws before risky changes." in rendered
    assert "Avoids drift from confirmed project rules." in rendered


def test_gen_laws_tolerates_empty_law_content():
    rendered = docs_service._gen_laws([
        {
            "content": "",
            "meta": {},
        }
    ])
    assert "Untitled law" in rendered
    assert "_No statement recorded._" in rendered


@pytest.mark.asyncio
async def test_rebuild_docs_includes_runtime_hints_and_tasks_sections(monkeypatch):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-candidates-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    try:
        async def fake_snapshot(*args, **kwargs):
            return {
                "project_id": "alpha",
                "components": [{"name": "Context", "component_id": "context", "purpose": "Purpose"}],
                "laws": [],
                "improvements": [],
                "runtime_hints": [{
                    "label": "context assembly",
                    "content": "Use unified retrieval before reading code.",
                    "confidence": 0.8,
                    "evidence_count": 4,
                }],
                "memoirs": [],
                "tasks": [{
                    "title": "Implement unified retrieval",
                    "status": "active",
                    "latest_change_type": "implementation",
                    "latest_change_summary": "Added task/improvement/runtime-hint context assembly.",
                }],
            }

        async def fake_fetch_skills(*args, **kwargs):
            return []

        async def fake_gen_overview(*args, **kwargs):
            return "**alpha**"

        async def fake_gen_architecture(*args, **kwargs):
            return "Architecture"

        monkeypatch.setattr(docs_service, "gather_project_knowledge_snapshot", fake_snapshot)
        monkeypatch.setattr(docs_service, "_fetch_skills", fake_fetch_skills)
        monkeypatch.setattr(docs_service, "_gen_overview", fake_gen_overview)
        monkeypatch.setattr(docs_service, "_gen_architecture", fake_gen_architecture)
        monkeypatch.setattr(docs_service, "_gen_features", lambda improvements: "Features")
        monkeypatch.setattr(docs_service, "_gen_pending", lambda improvements: "Pending")
        monkeypatch.setattr(docs_service, "_gen_decisions", lambda memoirs: "Decisions")
        monkeypatch.setattr(docs_service, "_gen_skills", lambda skills: "Skills")
        monkeypatch.setattr(docs_service, "_gen_performance", lambda project: "Performance")

        status = await docs_service.rebuild_docs("alpha", object(), "ignored", force=True)
        assert "runtime_hints" in status.sections
        assert "tasks" in status.sections
        assert "context assembly" in status.sections["runtime_hints"].content
        assert "Implement unified retrieval" in status.sections["tasks"].content
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_project_has_any_docs_source_uses_sqlite_and_learning_layers(monkeypatch):
    class FakeQdrant:
        async def count(self, *args, **kwargs):
            return SimpleNamespace(count=0)

        async def get_collections(self):
            return SimpleNamespace(collections=[])

    class FakeImprovementStore:
        async def list(self, project=None, status=None, limit=1):
            assert project == "alpha"
            return [{"id": "imp-1"}]

    monkeypatch.setattr(docs_service, "get_improvements_store", lambda: FakeImprovementStore())
    has_data = await docs_service._project_has_any_docs_source(FakeQdrant(), "memories", "alpha")
    assert has_data is True


@pytest.mark.asyncio
async def test_project_has_any_docs_source_short_circuits_on_component_docs_store(monkeypatch):
    class FakeQdrant:
        async def count(self, *args, **kwargs):
            raise AssertionError("Qdrant count should not run when component docs already exist")

        async def get_collections(self):
            raise AssertionError("Qdrant collections should not run when component docs already exist")

    class FakeImprovementStore:
        async def list(self, project=None, status=None, limit=1):
            return []

    class FakeLearningStore:
        async def list_artifacts(self, **kwargs):
            return []

    class FakeComponentDocsStore:
        async def list_by_project(self, project_id: str, limit: int = 1):
            assert project_id == "alpha"
            assert limit == 1
            return [{"id": "comp-1", "project_id": "alpha"}]

    monkeypatch.setattr(docs_service, "get_improvements_store", lambda: FakeImprovementStore())
    monkeypatch.setattr(docs_service, "get_learning_store", lambda: FakeLearningStore())
    monkeypatch.setattr(docs_service, "get_component_docs_store", lambda: FakeComponentDocsStore())

    has_data = await docs_service._project_has_any_docs_source(FakeQdrant(), "memories", "alpha")
    assert has_data is True


@pytest.mark.asyncio
async def test_cleanup_orphaned_caches_keeps_cache_when_component_docs_exist(monkeypatch):
    old_cache_dir = docs_service._CACHE_DIR
    local_tmp = Path("pytest_temp_local") / f"docs-cache-{uuid.uuid4().hex}"
    docs_service._CACHE_DIR = local_tmp / "docs_cache"
    docs_service._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = docs_service._CACHE_DIR / "alpha.json"
    cache_file.write_text('{"project":"alpha"}', encoding="utf-8")

    class FakeQdrant:
        async def count(self, *args, **kwargs):
            return SimpleNamespace(count=0)

        async def get_collections(self):
            return SimpleNamespace(collections=[])

    class FakeImprovementStore:
        async def list(self, project=None, status=None, limit=1):
            return []

    class FakeLearningStore:
        async def list_artifacts(self, **kwargs):
            return []

    class FakeComponentDocsStore:
        async def list_by_project(self, project_id: str, limit: int = 1):
            if project_id == "alpha":
                return [{"id": "comp-1"}]
            return []

    try:
        monkeypatch.setattr(docs_service, "get_improvements_store", lambda: FakeImprovementStore())
        monkeypatch.setattr(docs_service, "get_learning_store", lambda: FakeLearningStore())
        monkeypatch.setattr(docs_service, "get_component_docs_store", lambda: FakeComponentDocsStore())

        removed = await docs_service.cleanup_orphaned_caches(FakeQdrant(), "memories")
        assert removed == 0
        assert cache_file.exists() is True
    finally:
        docs_service._CACHE_DIR = old_cache_dir
        shutil.rmtree(local_tmp, ignore_errors=True)
