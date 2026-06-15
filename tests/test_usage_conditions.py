from pathlib import Path


def test_usage_conditions_doc_contains_public_release_basics():
    path = Path(__file__).resolve().parents[1] / "docs" / "USAGE_CONDITIONS.md"
    text = path.read_text(encoding="utf-8")

    assert "not legal advice" in text
    assert "Do not publish live service data" in text
    assert "Docker build contexts must exclude local caches" in text
    assert "public environment template" in text


def test_public_release_checklist_contains_operator_release_steps():
    path = Path(__file__).resolve().parents[1] / "docs" / "PUBLIC_RELEASE_CHECKLIST.md"
    text = path.read_text(encoding="utf-8")

    assert "python -m scripts.bootstrap_public_release --check" in text
    assert "python scripts/audit_release_artifacts.py" in text
    assert "python -m scripts.publish_docker_image" in text
    assert "No. Public releases must use synthetic or redacted data only." in text
