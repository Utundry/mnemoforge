from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import main as app_main
from app.core.exceptions import QdrantServiceError


@pytest.mark.asyncio
async def test_qdrant_startup_retries_transient_failures(monkeypatch):
    monkeypatch.setattr(app_main.settings, "qdrant_in_memory", False, raising=False)
    monkeypatch.setenv("QDRANT_STARTUP_RETRIES", "3")
    monkeypatch.setenv("QDRANT_STARTUP_RETRY_SECONDS", "0.1")

    sleep_mock = AsyncMock()
    monkeypatch.setattr(app_main.asyncio, "sleep", sleep_mock)

    class FakeQdrantService:
        def __init__(self) -> None:
            self.calls = 0

        async def ensure_collection(self) -> None:
            self.calls += 1
            if self.calls < 2:
                raise RuntimeError("temporary connection failure")

    svc = FakeQdrantService()

    await app_main._ensure_qdrant_ready(svc)

    assert svc.calls == 2
    assert sleep_mock.await_count == 1


@pytest.mark.asyncio
async def test_qdrant_startup_does_not_retry_configuration_errors(monkeypatch):
    monkeypatch.setattr(app_main.settings, "qdrant_in_memory", False, raising=False)
    monkeypatch.setenv("QDRANT_STARTUP_RETRIES", "3")
    monkeypatch.setenv("QDRANT_STARTUP_RETRY_SECONDS", "0.1")

    sleep_mock = AsyncMock()
    monkeypatch.setattr(app_main.asyncio, "sleep", sleep_mock)

    class FakeQdrantService:
        def __init__(self) -> None:
            self.calls = 0

        async def ensure_collection(self) -> None:
            self.calls += 1
            raise QdrantServiceError("collection dimension mismatch")

    svc = FakeQdrantService()

    with pytest.raises(QdrantServiceError):
        await app_main._ensure_qdrant_ready(svc)

    assert svc.calls == 1
    assert sleep_mock.await_count == 0
