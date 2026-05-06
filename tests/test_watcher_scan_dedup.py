from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_dir_parser import ParsedConversation
from app.services.watcher_service import (
    _analyze_conversation,
    _inflight_conversation_hashes,
    _processed_conversation_hashes,
)

PREFIX = "/api/v1"


@pytest.mark.asyncio
async def test_watcher_scan_skips_duplicates_by_file_hash(client, tmp_path):
    # Create a markdown file that will be split into multiple chunks
    md = (
        "# Title\n\n"
        "This is a sufficiently long section that should be ingested as a chunk.\n\n"
        "## Next\n\n"
        "This is another sufficiently long section that should also be ingested.\n"
    )
    (tmp_path / "notes.md").write_text(md, encoding="utf-8")

    first = await client.post(f"{PREFIX}/watcher/scan", json={
        "dirs": [str(tmp_path)],
        "agent_id": "watcher-test",
        "max_files": 50,
        "dry_run": False,
    })
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["chunks_found"] >= 1
    assert first_body["chunks_stored"] == first_body["chunks_found"]
    assert first_body["skipped_duplicates"] == 0

    second = await client.post(f"{PREFIX}/watcher/scan", json={
        "dirs": [str(tmp_path)],
        "agent_id": "watcher-test",
        "max_files": 50,
        "dry_run": False,
    })
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["chunks_found"] == first_body["chunks_found"]
    assert second_body["chunks_stored"] == 0
    assert second_body["skipped_duplicates"] == second_body["chunks_found"]


@pytest.mark.asyncio
async def test_watcher_scan_analyzes_dialogue_history_and_creates_review_candidate(client, tmp_path):
    convo = tmp_path / "session.jsonl"
    messages = [
        {"role": "user", "content": "I need help configuring nginx for reverse proxy and SSL."},
        {"role": "assistant", "content": "I am not sure about nginx specifics here."},
        {"role": "user", "content": "Please remember that concise answers work best for me."},
        {"role": "assistant", "content": "Understood, I will keep it concise."},
    ]
    convo.write_text("\n".join(json.dumps(m) for m in messages), encoding="utf-8")

    signal = json.dumps({
        "new_terminology": [],
        "missing_skill": ["nginx"],
        "domain_drift": [],
        "user_preference": ["prefers concise answers"],
        "successful_pattern": ["keep answers concise after clarification"],
    })

    with patch("app.routers.skills._llm", new=AsyncMock(return_value=signal)):
        response = await client.post(f"{PREFIX}/watcher/scan", json={
            "dirs": [str(tmp_path)],
            "agent_id": "watcher-dialogue-test",
            "max_files": 50,
            "dry_run": False,
        })

    assert response.status_code == 200

    improvements = await client.get("/api/v1/artifacts?project=mnemoforge&type=improvement&artifact_status=open&limit=50")
    assert improvements.status_code == 200
    data = improvements.json()
    items = data.get("items", [])
    titles = [item["title"].lower() for item in items]
    assert any("nginx" in title for title in titles)

    artifacts = await client.get("/api/v1/learning/artifacts?scope=candidate&status=pending_review&limit=50")
    assert artifacts.status_code == 200
    pending = artifacts.json()["artifacts"]
    assert any("dialogue-analysis" in (item.get("tags") or []) for item in pending)
    assert any("nginx" in (item.get("observation") or "").lower() for item in pending)


@pytest.mark.asyncio
async def test_watcher_service_dedups_concurrent_same_file_hash():
    _processed_conversation_hashes.clear()
    _inflight_conversation_hashes.clear()
    convo = ParsedConversation(
        transcript="USER: need qdrant help\nASSISTANT: missing guidance",
        source_path="C:/tmp/live-selfinit.jsonl",
        file_hash="same-hash",
        session_id="sess-1",
        user_messages=1,
        assistant_messages=1,
    )

    calls = AsyncMock()

    async def _fake_analyze(**_):
        await asyncio.sleep(0.01)

    calls.side_effect = _fake_analyze
    with patch("app.routers.skills.analyze_dialogue_transcript", new=calls):
        await asyncio.gather(
            _analyze_conversation(convo, "ai-dirs"),
            _analyze_conversation(convo, "ai-dirs"),
        )

    assert calls.await_count == 1
    assert _processed_conversation_hashes[convo.source_path] == convo.file_hash
    assert not _inflight_conversation_hashes
