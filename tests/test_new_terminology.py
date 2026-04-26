from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app.services.new_terminology import (
    sanitize_new_terminology_candidate,
    sanitize_successful_pattern_candidate,
    should_reclassify_as_missing_skill,
)
from tests.conftest import _build_client, MOCK_VECTOR


@pytest_asyncio.fixture
async def client():
    c, qdrant_client, _ = await _build_client(MOCK_VECTOR)
    async with c:
        yield c
    await qdrant_client.close()


def test_sanitize_new_terminology_candidate_keeps_conceptual_terms():
    assert sanitize_new_terminology_candidate("memory shard taxonomy") == "memory shard taxonomy"
    assert sanitize_new_terminology_candidate("tenant consistency model") == "tenant consistency model"


def test_sanitize_new_terminology_candidate_filters_procedural_phrases_in_procedural_context():
    transcript = (
        "Need operational guidance for rollback verification and storage recovery. "
        "This is a missing operational skill, not new terminology."
    )
    assert sanitize_new_terminology_candidate("storage recovery", transcript=transcript) is None
    assert sanitize_new_terminology_candidate("rollback verification", transcript=transcript) is None
    assert should_reclassify_as_missing_skill("rollback verification", transcript=transcript) is True


def test_sanitize_successful_pattern_candidate_requires_evidence_of_success():
    transcript = "USER: This worked well and solved the deployment issue. ASSISTANT: We should reuse this rollout checklist."
    assert sanitize_successful_pattern_candidate("reuse rollout checklist", transcript=transcript) == "reuse rollout checklist"
    assert sanitize_successful_pattern_candidate(
        "acknowledging knowledge gaps",
        transcript="USER: We need more guidance. ASSISTANT: I do not know yet.",
    ) is None


@pytest.mark.asyncio
async def test_dialogue_analyze_reclassifies_procedural_terms_from_new_terminology(client):
    llm_signal = json.dumps({
        "new_terminology": ["storage recovery", "rollback verification", "memory shard taxonomy"],
        "missing_skill": ["operational guidance"],
        "domain_drift": [],
        "user_preference": [],
        "successful_pattern": [],
    })
    transcript = (
        "USER: We need operational guidance for storage recovery and rollback verification.\n"
        "ASSISTANT: This is a missing operational skill, not new terminology.\n"
        "USER: We still need a shared language for tenant memory shard taxonomy.\n"
    )
    with patch("app.routers.skills._llm", new=AsyncMock(return_value=llm_signal)):
        r = await client.post("/api/v1/skills/dialogue/analyze", json={
            "transcript": transcript,
            "agent_id": "test",
        })

    assert r.status_code == 200
    body = r.json()
    assert body["signals"]["new_terminology"] == ["memory shard taxonomy"]
    assert "storage recovery" in body["signals"]["missing_skill"]
    assert "rollback verification" in body["signals"]["missing_skill"]
