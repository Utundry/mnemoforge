import pytest

from app import main as app_main


def test_local_warmup_provider_selection_can_target_lmstudio_only(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "lmstudio")
    monkeypatch.setenv("LOCAL_LLM_FALLBACK_ORDER", "lmstudio")
    monkeypatch.setenv("OLLAMA_WARMUP_ENABLED", "1")
    monkeypatch.setenv("LMSTUDIO_WARMUP_ENABLED", "1")

    assert app_main._ollama_warmup_enabled() is False
    assert app_main._lmstudio_warmup_enabled() is True


def test_local_warmup_provider_selection_can_disable_all_local_warmups(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "auto")
    monkeypatch.setenv("LOCAL_LLM_FALLBACK_ORDER", "ollama,lmstudio")
    monkeypatch.setenv("OLLAMA_WARMUP_ENABLED", "0")
    monkeypatch.setenv("LMSTUDIO_WARMUP_ENABLED", "0")

    assert app_main._ollama_warmup_enabled() is False
    assert app_main._lmstudio_warmup_enabled() is False


@pytest.mark.asyncio
async def test_local_warmup_does_not_raise_on_dimension_mismatch(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "auto")
    monkeypatch.setenv("LOCAL_LLM_FALLBACK_ORDER", "ollama,lmstudio")
    monkeypatch.setenv("OLLAMA_WARMUP_ENABLED", "1")
    monkeypatch.setenv("LMSTUDIO_WARMUP_ENABLED", "1")

    async def fake_ollama_warmup(_ollama):
        return 123

    async def fake_lmstudio_warmup():
        return 456

    monkeypatch.setattr(app_main, "_warmup_ollama_embeddings", fake_ollama_warmup)
    monkeypatch.setattr(app_main, "_warmup_lmstudio_embeddings", fake_lmstudio_warmup)

    await app_main._warmup_local_embedding_services(object())
