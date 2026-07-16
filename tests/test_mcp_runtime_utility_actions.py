import pytest

from app.services.mcp_runtime_utility_actions import (
    RuntimeUtilityActionDependencies,
    execute_runtime_utility_action,
)


@pytest.mark.asyncio
async def test_system_info_summarizes_any_usable_llm_provider() -> None:
    async def fake_get(api_base: str, path: str):
        assert path == "/system/info"
        return {
            "status": "ok",
            "uptime_seconds": 180,
            "infrastructure": {
                "qdrant": {"reachable": True},
                "ollama": {"reachable": False, "models": []},
                "embedding_model": "nomic-embed-text",
                "embedding_dimensions": 768,
                "llm_providers": {
                    "healthy": True,
                    "usable_providers": ["lmstudio"],
                    "available_llms": [
                        {"id": "local-model", "provider": "lmstudio", "kind": "local_openai_compatible"}
                    ],
                    "providers": {
                        "ollama": {"enabled": True, "reachable": False, "kind": "local"},
                        "lmstudio": {"enabled": True, "reachable": True, "kind": "local_openai_compatible"},
                    },
                },
            },
            "counters": {"memories": 3, "skills": 2, "layout_terms": 1},
            "components": [],
        }

    async def fake_post(api_base: str, path: str, payload: dict):
        raise AssertionError("system_info should not post")

    text = await execute_runtime_utility_action(
        name="system_info",
        args={},
        api_base="http://test",
        dependencies=RuntimeUtilityActionDependencies(get=fake_get, post=fake_post),
    )

    assert "LLM providers: ok" in text
    assert "usable: lmstudio" in text
    assert "embedding: lmstudio/nomic-embed-text" in text
    assert "Ollama: fail" not in text