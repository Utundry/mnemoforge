from pathlib import Path

from app.models.law import ProjectLawRecord
from app.services import knowledge_projection_service as service


def _sample_law(*, updated_at: str, rationale: str = "Important rationale") -> ProjectLawRecord:
    return ProjectLawRecord(
        id="law-1",
        project="alpha",
        scope="project",
        status="active",
        title="Require review",
        statement="Agents must review active laws before risky changes.",
        rationale=rationale,
        evidence=[],
        tags=[],
        topic_path="laws/require-review",
        source="project-law",
        created_at=__import__("datetime").datetime.fromisoformat("2026-03-23T09:00:00+00:00"),
        updated_at=__import__("datetime").datetime.fromisoformat(updated_at),
        memory_id="law-1",
        is_project_local=True,
    )


def test_compact_law_projection_is_cached_and_reused(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "knowledge_projections.db")
    law = _sample_law(updated_at="2026-03-23T09:10:00+00:00")
    first = service.get_or_build_law_projection(law, variant="compact")
    second = service.get_or_build_law_projection(law, variant="compact")
    assert first["content"] == second["content"]
    assert first["generated_at"] == second["generated_at"]
    assert "Require review" in first["content"]


def test_law_projection_block_supports_compact_variant(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "knowledge_projections.db")
    law = _sample_law(updated_at="2026-03-23T09:10:00+00:00", rationale="This keeps risky work aligned with reviewed project rules.")
    block = service.build_law_projection_block([law], variant="compact")
    assert "## Applicable Project Laws" in block
    assert "Require review" in block
    assert "Why:" in block
