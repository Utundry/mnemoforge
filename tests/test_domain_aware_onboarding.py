"""
Tests for domain-aware onboarding features:
  1. Pinned skills — GET /skills/pinned, PATCH /skills/{id}/pin
  2. Domain inference — POST /skills/infer-domain
  3. tier=reference in POST /router/decide
"""
from __future__ import annotations

import pytest
import pytest_asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


async def _publish_skill(client, name: str, *, pinned: bool = False, reference_url: str | None = None):
    payload = {
        "name": name,
        "content": f"# {name}\n\nTest skill content for {name}.",
        "platform": "claude",
        "agent_id": "test-agent",
        "description": f"Test skill {name}",
        "domain_tags": ["testing", "python"],
        "pinned": pinned,
    }
    if reference_url:
        payload["reference_url"] = reference_url
    resp = await client.post("/api/v1/skills/publish", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Feature 1: Pinned skills ──────────────────────────────────────────────────


class TestPinnedSkills:
    @pytest.mark.asyncio
    async def test_publish_with_pinned_flag(self, client):
        """Publishing a skill with pinned=True stores the flag."""
        skill = await _publish_skill(client, "emergency-contact", pinned=True, reference_url="tel:112")
        assert skill["pinned"] is True
        assert skill["reference_url"] == "tel:112"

    @pytest.mark.asyncio
    async def test_publish_default_not_pinned(self, client):
        """Skills published without pinned flag default to pinned=False."""
        skill = await _publish_skill(client, "regular-skill")
        assert skill["pinned"] is False
        assert skill["reference_url"] is None

    @pytest.mark.asyncio
    async def test_get_pinned_returns_only_pinned(self, client):
        """GET /skills/pinned returns only pinned skills."""
        pinned = await _publish_skill(client, "pinned-one", pinned=True)
        await _publish_skill(client, "not-pinned-one", pinned=False)

        resp = await client.get("/api/v1/skills/pinned")
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()]
        assert pinned["id"] in ids

    @pytest.mark.asyncio
    async def test_get_pinned_excludes_unpinned(self, client):
        """GET /skills/pinned does not include non-pinned skills."""
        unpinned = await _publish_skill(client, "unpinned-two", pinned=False)

        resp = await client.get("/api/v1/skills/pinned")
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()]
        assert unpinned["id"] not in ids

    @pytest.mark.asyncio
    async def test_patch_pin_pins_skill(self, client):
        """PATCH /skills/{id}/pin pins an existing skill."""
        skill = await _publish_skill(client, "to-pin")
        assert skill["pinned"] is False

        resp = await client.patch(f"/api/v1/skills/{skill['id']}/pin?pinned=true")
        assert resp.status_code == 200
        assert resp.json()["pinned"] is True

    @pytest.mark.asyncio
    async def test_patch_pin_unpins_skill(self, client):
        """PATCH /skills/{id}/pin?pinned=false unpins a skill."""
        skill = await _publish_skill(client, "to-unpin", pinned=True)

        resp = await client.patch(f"/api/v1/skills/{skill['id']}/pin?pinned=false")
        assert resp.status_code == 200
        assert resp.json()["pinned"] is False

    @pytest.mark.asyncio
    async def test_patch_pin_nonexistent_skill_404(self, client):
        """PATCH /skills/{id}/pin returns 404 for unknown skill ID."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        resp = await client.patch(f"/api/v1/skills/{fake_id}/pin")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_pinned_skill_has_reference_url(self, client):
        """A pinned skill with reference_url appears in /skills/pinned with that URL."""
        ref_url = "https://emergency.example.com"
        skill = await _publish_skill(client, "emergency-ref", pinned=True, reference_url=ref_url)

        resp = await client.get("/api/v1/skills/pinned")
        assert resp.status_code == 200
        found = next((s for s in resp.json() if s["id"] == skill["id"]), None)
        assert found is not None
        assert found["reference_url"] == ref_url


# ── Feature 2: Domain inference ───────────────────────────────────────────────


class TestDomainInference:
    @pytest.mark.asyncio
    async def test_infer_domain_no_history_returns_general(self, client):
        """Agent with no history gets domain=general."""
        resp = await client.post("/api/v1/skills/infer-domain", json={
            "agent_id": "brand-new-agent-xyz",
            "session_hints": [],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["domain"] == "general"
        assert data["confidence"] <= 0.2

    @pytest.mark.asyncio
    async def test_infer_domain_from_memory_tags(self, client):
        """Agent with python-tagged memories infers python domain."""
        # Store memory with python tag
        mem_resp = await client.post("/api/v1/memories", json={
            "content": "Working on Python async code",
            "agent_id": "python-agent",
            "memory_type": "context",
            "tags": ["python", "testing"],
        })
        assert mem_resp.status_code == 201

        resp = await client.post("/api/v1/skills/infer-domain", json={
            "agent_id": "python-agent",
            "session_hints": [],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["domain"] in ("python", "testing")
        assert data["confidence"] > 0.1

    @pytest.mark.asyncio
    async def test_infer_domain_session_hints_weighted(self, client):
        """Session hints contribute to domain inference."""
        resp = await client.post("/api/v1/skills/infer-domain", json={
            "agent_id": "hint-agent",
            "session_hints": ["python", "testing", "memory_search"],
        })
        assert resp.status_code == 200
        data = resp.json()
        # python/testing should appear in all_domains
        all_domains = data["all_domains"]
        assert any(d in all_domains for d in ("python", "testing"))

    @pytest.mark.asyncio
    async def test_infer_domain_returns_signals(self, client):
        """Response includes signals explaining the inference."""
        await client.post("/api/v1/memories", json={
            "content": "Deployed to docker container",
            "agent_id": "docker-agent",
            "memory_type": "fact",
            "tags": ["docker", "deploy"],
        })
        resp = await client.post("/api/v1/skills/infer-domain", json={
            "agent_id": "docker-agent",
            "session_hints": [],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["signals"], list)

    @pytest.mark.asyncio
    async def test_infer_domain_all_domains_list(self, client):
        """all_domains contains multiple domains sorted by frequency."""
        for tag_pair in [["python", "testing"], ["python", "api"], ["python", "backend"]]:
            await client.post("/api/v1/memories", json={
                "content": "Some work",
                "agent_id": "multi-domain-agent",
                "memory_type": "fact",
                "tags": tag_pair,
            })

        resp = await client.post("/api/v1/skills/infer-domain", json={
            "agent_id": "multi-domain-agent",
            "session_hints": [],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "python" in data["all_domains"]  # most frequent
        assert data["domain"] == "python"


# ── Feature 3: tier=reference in route_task ───────────────────────────────────


class TestReferenceRouting:
    @pytest.mark.asyncio
    async def test_unknown_task_type_returns_reference_tier(self, client):
        """A task type with no capability data triggers tier=reference."""
        resp = await client.post("/api/v1/router/decide", json={
            "task": "Perform open heart surgery on the patient",
            "task_type": "nonexistent_task_type_xyz",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "reference"
        assert data["component"] == "reference"

    @pytest.mark.asyncio
    async def test_reference_tier_includes_pinned_references(self, client):
        """When tier=reference, pinned skills with reference_url appear in references."""
        # Publish a pinned reference skill
        ref = await _publish_skill(
            client, "emergency-service",
            pinned=True,
            reference_url="tel:112",
        )

        resp = await client.post("/api/v1/router/decide", json={
            "task": "Medical emergency — patient is unconscious",
            "task_type": "nonexistent_task_type_xyz",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "reference"
        ref_ids = [r["id"] for r in data.get("references", [])]
        assert ref["id"] in ref_ids

    @pytest.mark.asyncio
    async def test_reference_tier_references_have_url(self, client):
        """References returned with tier=reference include reference_url."""
        await _publish_skill(
            client, "helpdesk-phone",
            pinned=True,
            reference_url="tel:+18005551234",
        )

        resp = await client.post("/api/v1/router/decide", json={
            "task": "Fix patient bleeding",
            "task_type": "nonexistent_task_type_xyz",
        })
        assert resp.status_code == 200
        data = resp.json()
        if data["tier"] == "reference":
            ref_urls = [r.get("reference_url") for r in data.get("references", []) if r.get("reference_url")]
            assert any(url for url in ref_urls)

    @pytest.mark.asyncio
    async def test_reference_tier_confidence_is_zero(self, client):
        """Out-of-domain routing has confidence=0."""
        resp = await client.post("/api/v1/router/decide", json={
            "task": "Nothing we handle",
            "task_type": "nonexistent_task_type_xyz",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "reference"
        assert data["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_decide_response_has_references_field(self, client):
        """DecideResponse always includes references field (empty for non-reference tiers)."""
        resp = await client.post("/api/v1/router/decide", json={
            "task": "Fix a Python syntax error",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "references" in data
        assert isinstance(data["references"], list)
