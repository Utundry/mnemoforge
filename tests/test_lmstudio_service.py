from __future__ import annotations

import httpx
import pytest

from app.services.lmstudio_service import LMStudioService


@pytest.mark.asyncio
async def test_lmstudio_resolve_model_prefers_first_non_embedding_model(monkeypatch):
    async def fake_get(self, url, *, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "object": "list",
                "data": [
                    {"id": "text-embedding-nomic-embed-text-v1.5"},
                    {"id": "qwen/qwen3-1.7b"},
                    {"id": "google/gemma-4-e2b"},
                ],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    service = LMStudioService(model="auto")
    try:
        assert await service.resolve_model() == "qwen/qwen3-1.7b"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_lmstudio_generate_uses_resolved_model(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_get(self, url, *, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"id": "qwen/qwen3-1.7b"}]},
        )

    async def fake_post(self, url, *, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "local ok"}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    service = LMStudioService(base_url="http://127.0.0.1:1234/v1", model="auto")
    try:
        result = await service.generate("ping")
    finally:
        await service.close()

    assert result == "local ok"
    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert captured["json"]["model"] == "qwen/qwen3-1.7b"


@pytest.mark.asyncio
async def test_lmstudio_embed_uses_embedding_endpoint_and_model(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_get(self, url, *, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "data": [
                    {"id": "qwen/qwen3-1.7b"},
                    {"id": "text-embedding-nomic-embed-text-v1.5"},
                ]
            },
        )

    async def fake_post(self, url, *, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"embedding": [0.1, 0.2, 0.3]}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    service = LMStudioService(base_url="http://127.0.0.1:1234/v1", model="auto")
    try:
        result = await service.embed("checkpoint")
    finally:
        await service.close()

    assert result == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://127.0.0.1:1234/v1/embeddings"
    assert captured["json"] == {
        "model": "text-embedding-nomic-embed-text-v1.5",
        "input": "checkpoint",
    }
