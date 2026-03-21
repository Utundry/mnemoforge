"""Regression tests for Skill Evolver (POST /crystallizer/evolve)."""
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


async def _publish_skill(client, name: str, tags: list[str]) -> str:
    r = await client.post("/api/v1/skills/publish", json={
        "name": name,
        "content": f"# {name}\n\nHandles {name}.\n\n## Instructions\nDo it.",
        "platform": "claude",
        "agent_id": "evolver-test",
        "description": f"{name} skill",
        "domain_tags": tags,
        "importance_score": 0.7,
    })
    assert r.status_code == 200
    return r.json()["id"]


class TestSkillEvolver:
    async def test_evolve_returns_structure(self, client):
        r = await client.post("/api/v1/crystallizer/evolve")
        assert r.status_code == 200
        body = r.json()
        assert "suppressed" in body
        assert "re_enabled" in body
        assert "gaps_detected" in body
        assert "total_skills" in body
        assert "evolved_at" in body

    async def test_evolve_suppressed_is_list(self, client):
        r = await client.post("/api/v1/crystallizer/evolve")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["suppressed"], list)
        assert isinstance(body["re_enabled"], list)
        assert isinstance(body["gaps_detected"], list)

    async def test_evolve_counts_total_skills(self, client):
        await _publish_skill(client, "evolver-skill-1", ["python"])
        await _publish_skill(client, "evolver-skill-2", ["python"])

        r = await client.post("/api/v1/crystallizer/evolve")
        assert r.status_code == 200
        body = r.json()
        assert body["total_skills"] >= 2

    async def test_evolve_does_not_suppress_new_skills(self, client):
        """Skills with usage_count < 10 must not be suppressed."""
        skill_id = await _publish_skill(client, "new-skill-no-suppress", ["typescript"])

        r = await client.post("/api/v1/crystallizer/evolve")
        assert r.status_code == 200
        body = r.json()
        assert skill_id not in body["suppressed"]

    async def test_evolve_idempotent(self, client):
        """Calling evolve twice should not raise errors."""
        r1 = await client.post("/api/v1/crystallizer/evolve")
        r2 = await client.post("/api/v1/crystallizer/evolve")
        assert r1.status_code == 200
        assert r2.status_code == 200

    async def test_evolve_detects_gap_from_outcome(self, client):
        """If outcome records mention missing_domains repeatedly, evolve detects gap."""
        # Record 3 outcomes with missing domain 'rust'
        for i in range(3):
            await client.post("/api/v1/skills/outcome", json={
                "pack_id": f"fake-pack-{i}",
                "skills_helpful": [],
                "skills_unused": [],
                "missing_domains": ["rust"],
                "success": True,
                "agent_id": "test",
            })

        r = await client.post("/api/v1/crystallizer/evolve")
        assert r.status_code == 200
        body = r.json()
        # 'rust' should appear as a detected gap (no rust skill published)
        assert "rust" in body["gaps_detected"]

    async def test_evolve_no_gap_when_skill_exists(self, client):
        """If a skill for the missing domain already exists, it should not appear as a gap."""
        await _publish_skill(client, "rust-skill", ["rust"])

        for i in range(3):
            await client.post("/api/v1/skills/outcome", json={
                "pack_id": f"rust-pack-{i}",
                "skills_helpful": [],
                "skills_unused": [],
                "missing_domains": ["rust"],
                "success": True,
                "agent_id": "test",
            })

        r = await client.post("/api/v1/crystallizer/evolve")
        assert r.status_code == 200
        body = r.json()
        assert "rust" not in body["gaps_detected"]
