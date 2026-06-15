from app.config import settings
from app.services.project_capability_service import evaluate_capability_tags


def test_capability_tags_are_available_when_runtime_declares_capability(monkeypatch):
    monkeypatch.setattr(settings, "project_capabilities", "repository-development-tools,other")

    result = evaluate_capability_tags(
        ["project-law", "requires-capability:repository-development-tools"]
    )

    assert result["status"] == "available"
    assert result["missing_capabilities"] == []


def test_capability_tags_are_unavailable_without_required_runtime_capability(monkeypatch):
    monkeypatch.setattr(settings, "project_capabilities", "")

    result = evaluate_capability_tags(
        ["requires-capability:repository-development-tools"]
    )

    assert result["status"] == "unavailable"
    assert result["missing_capabilities"] == ["repository-development-tools"]
    assert "not installed" in str(result["reason"])
