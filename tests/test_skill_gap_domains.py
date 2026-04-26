from __future__ import annotations

from pathlib import Path

import pytest

from app.services.improvements_store import ImprovementsStore
from app.services.skill_gap_domains import (
    canonicalize_skill_gap_domain,
    canonicalize_skill_gap_title,
    infer_skill_gap_domains_from_transcript,
    refine_skill_gap_domains,
)


def test_canonicalize_skill_gap_domain_aliases():
    assert canonicalize_skill_gap_domain("API_Setup") == "api configuration"
    assert canonicalize_skill_gap_domain("Diagnostics") == "troubleshooting"
    assert canonicalize_skill_gap_domain("nginx") == "nginx"
    assert canonicalize_skill_gap_domain("rollback verification") == "rollback verification"
    assert canonicalize_skill_gap_domain("storage recovery") == "storage recovery"
    assert canonicalize_skill_gap_domain("tenant isolation") == "tenant isolation"


def test_canonicalize_skill_gap_domain_filters_generic_labels():
    assert canonicalize_skill_gap_domain("operational") is None
    assert canonicalize_skill_gap_domain("operational guidance") is None
    assert canonicalize_skill_gap_domain("recovery") is None
    assert canonicalize_skill_gap_domain("system optimization") is None
    assert canonicalize_skill_gap_domain("service setup") is None
    assert canonicalize_skill_gap_domain("memory management") is None
    assert canonicalize_skill_gap_domain("database operations") is None


def test_canonicalize_skill_gap_title():
    assert canonicalize_skill_gap_title("Skill gap detected: API_Config") == "Skill gap detected: api configuration"
    assert canonicalize_skill_gap_title("Skill gap detected: system optimization") is None


def test_refine_skill_gap_domains_dedupes_and_filters_generic_labels():
    refined = refine_skill_gap_domains(
        ["storage recovery", "Storage Recovery", "database operations", "rollback verification"],
        "Need a recovery runbook.",
    )
    assert refined == ["storage recovery", "rollback verification"]


def test_infer_skill_gap_domains_from_transcript_is_conservative():
    inferred = infer_skill_gap_domains_from_transcript("Need a runbook for recovery and validation.")
    assert inferred == []


@pytest.mark.asyncio
async def test_improvements_store_dedups_skill_gap_alias_titles(tmp_path: Path):
    store = ImprovementsStore(tmp_path / "improvements.db")
    try:
        first_id, first_created = await store.upsert_by_title(
            title="Skill gap detected: api_config",
            description="first",
            project="supermemory",
            agent_id="test",
            tags=["skill-gap"],
        )
        second_id, second_created = await store.upsert_by_title(
            title="Skill gap detected: API setup",
            description="second",
            project="supermemory",
            agent_id="test",
            tags=["skill-gap", "auto-detected"],
        )

        assert first_created is True
        assert second_created is False
        assert second_id == first_id

        rows = await store.list(project="supermemory", status="open", limit=10)
        matching = [row for row in rows if row["title"].lower().startswith("skill gap detected:")]
        assert len(matching) == 1
        assert matching[0]["norm_title"] == "skill gap detected api configuration"
    finally:
        store.close()
