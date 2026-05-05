"""Regression tests for outcome-driven importance feedback (improvement d79d5697...)."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_outcome_success_boosts_used_memory_importance(client):
    # Create a memory
    r = await client.post("/api/v1/memories", json={
        "content": "Run tests before commit",
        "agent_id": "agent",
        "memory_type": "fact",
        "category": "general",
        "importance_score": 0.5,
    })
    assert r.status_code == 201
    mid = r.json()["id"]

    # Use it via /context and link to session
    sess = "sess-success-1"
    ctx = await client.post("/api/v1/memories/context", json={
        "query": "tests commit",
        "agent_id": "agent",
        "limit": 5,
        "max_tokens": 200,
        "format": "text",
        "session_id": sess,
    })
    assert ctx.status_code == 200
    assert ctx.json().get("session_id") == sess

    # Record success outcome for that session
    out = await client.post("/api/v1/outcomes", json={
        "success": True,
        "session_id": sess,
    })
    assert out.status_code == 200
    body = out.json()
    assert body["updated"] >= 1
    assert mid in body["memory_ids"]

    # Importance should increase
    m = await client.get(f"/api/v1/memories/{mid}")
    assert m.status_code == 200
    assert m.json()["importance_score"] > 0.5


@pytest.mark.asyncio
async def test_outcome_failure_penalizes_used_memory_importance(client):
    r = await client.post("/api/v1/memories", json={
        "content": "Use typed dicts for configs",
        "agent_id": "agent",
        "memory_type": "fact",
        "category": "general",
        "importance_score": 0.6,
    })
    assert r.status_code == 201
    mid = r.json()["id"]

    sess = "sess-fail-1"
    ctx = await client.post("/api/v1/memories/context", json={
        "query": "typed dict config",
        "agent_id": "agent",
        "limit": 5,
        "max_tokens": 200,
        "format": "text",
        "session_id": sess,
    })
    assert ctx.status_code == 200

    out = await client.post("/api/v1/outcomes", json={
        "success": False,
        "session_id": sess,
    })
    assert out.status_code == 200
    assert mid in out.json()["memory_ids"]

    m = await client.get(f"/api/v1/memories/{mid}")
    assert m.status_code == 200
    assert m.json()["importance_score"] < 0.6
