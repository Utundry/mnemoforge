from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.knowledge_tree import RoutingRule
from app.services import knowledge_tree as knowledge_tree_module
from app.services.knowledge_tree import KnowledgeTree, verify_tree_classification_handler


class _FakeRepo:
    def __init__(self, requires_llm: bool):
        self._rule = RoutingRule(pattern="jwt auth", requires_llm=requires_llm)

    def get_routing_rule(self, pattern: str):
        return self._rule


class _FakeSLM:
    def __init__(self, category: str):
        self.generate = AsyncMock(return_value=category)
        self.embed = AsyncMock(return_value=[0.1])


class _FakeJobQueue:
    def __init__(self):
        self.submit = AsyncMock()


async def test_classify_query_falls_back_to_slm_when_cloud_is_unavailable(monkeypatch):
    monkeypatch.setattr(knowledge_tree_module, "cloud_available", lambda: False)

    slm = _FakeSLM("python/fastapi/auth")
    llm = SimpleNamespace(generate=AsyncMock(return_value="cloud/path"))
    jobs = _FakeJobQueue()
    tree = KnowledgeTree(
        repo=_FakeRepo(requires_llm=True),
        slm=slm,
        llm_gateway=llm,
        job_queue=jobs,
        qdrant=SimpleNamespace(),
        scorer=SimpleNamespace(),
    )

    result = await tree.classify_query_adaptive("How do I configure JWT auth tokens?")

    assert result == "python/fastapi/auth"
    llm.generate.assert_not_awaited()
    jobs.submit.assert_not_awaited()


async def test_verify_tree_classification_skips_without_cloud(monkeypatch):
    monkeypatch.setattr(knowledge_tree_module, "cloud_available", lambda: False)

    result = await verify_tree_classification_handler(
        {"query": "jwt auth", "pattern": "jwt auth", "slm_category": "python/auth"}
    )

    assert result == {"status": "skipped", "reason": "cloud_unavailable"}
