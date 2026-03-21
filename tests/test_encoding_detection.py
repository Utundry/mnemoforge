"""Tests for encoding/terminal artifact detection (issue 6ce796af)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from tests.conftest import _build_client, MOCK_VECTOR


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


async def _analyze(client, text: str, agent_id: str = "enc-test"):
    r = await client.post("/api/v1/skills/encoding/analyze", json={
        "text": text,
        "agent_id": agent_id,
    })
    assert r.status_code == 200
    return r.json()


class TestEncodingAnalyze:
    async def test_clean_text_returns_no_artifacts(self, client):
        body = await _analyze(client, "Hello, this is normal UTF-8 text with no issues.")
        assert body["clean"] is True
        assert body["total"] == 0

    async def test_returns_required_fields(self, client):
        body = await _analyze(client, "clean text here")
        assert "artifacts" in body
        assert "total" in body
        assert "clean" in body
        assert "agent_id" in body

    async def test_unicode_decode_error_detected(self, client):
        body = await _analyze(client, "UnicodeDecodeError: 'charmap' codec can't decode byte")
        assert body["clean"] is False
        types = [a["signal_type"] for a in body["artifacts"]]
        assert "encoding_artifact" in types

    async def test_cp1251_reference_detected_as_shell_artifact(self, client):
        body = await _analyze(client, "encoding: cp1251 detected in config file")
        assert body["clean"] is False
        types = [a["signal_type"] for a in body["artifacts"]]
        assert "shell_artifact" in types

    async def test_windows_1251_detected(self, client):
        body = await _analyze(client, '<?xml version="1.0" encoding="windows-1251"?>')
        types = [a["signal_type"] for a in body["artifacts"]]
        assert "shell_artifact" in types

    async def test_chcp_command_detected(self, client):
        body = await _analyze(client, "run chcp 1251 to change code page")
        types = [a["signal_type"] for a in body["artifacts"]]
        assert "shell_artifact" in types

    async def test_box_drawing_detected_as_terminal_issue(self, client):
        body = await _analyze(client, "╔═══════╗\n║ table ║\n╚═══════╝")
        types = [a["signal_type"] for a in body["artifacts"]]
        assert "terminal_rendering_issue" in types

    async def test_artifact_has_required_fields(self, client):
        body = await _analyze(client, "UnicodeEncodeError in output stream")
        if body["artifacts"]:
            for field in ("signal_type", "action", "message", "confidence", "evidence"):
                assert field in body["artifacts"][0]

    async def test_deduped_per_signal_type(self, client):
        """Multiple matches of same type produce one artifact entry."""
        body = await _analyze(
            client,
            "UnicodeDecodeError here and also UnicodeEncodeError there",
        )
        enc_artifacts = [a for a in body["artifacts"] if a["signal_type"] == "encoding_artifact"]
        assert len(enc_artifacts) == 1

    async def test_normal_russian_utf8_is_clean(self, client):
        """Properly encoded UTF-8 Russian text must not trigger false positives."""
        body = await _analyze(client, "Привет мир, это нормальный UTF-8 текст")
        assert body["clean"] is True

    async def test_confidence_values_in_range(self, client):
        body = await _analyze(client, "cp1251 encoding problem with UnicodeDecodeError")
        for a in body["artifacts"]:
            assert 0.0 <= a["confidence"] <= 1.0
