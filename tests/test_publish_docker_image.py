from __future__ import annotations

from pathlib import Path

from scripts import publish_docker_image as helper


def test_validate_dockerignore_flags_missing_rules(tmp_path: Path):
    (tmp_path / ".dockerignore").write_text(".git/\n.env\n", encoding="utf-8")

    missing = helper._validate_dockerignore(tmp_path)

    assert ".venv/" in missing
    assert "qdrant_data/" in missing
    assert "logs/" in missing


def test_resolve_repository_prefers_argument_then_env(monkeypatch):
    monkeypatch.delenv("DOCKERHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("DOCKER_IMAGE_REPOSITORY", raising=False)
    monkeypatch.setenv("DOCKERHUB_REPOSITORY", "example/mnemoforge")

    assert helper._resolve_repository(None) == "example/mnemoforge"
    assert helper._resolve_repository("other/mnemoforge") == "other/mnemoforge"


def test_resolve_repository_rejects_tagged_value():
    try:
        helper._resolve_repository("example/mnemoforge:latest")
    except ValueError as exc:
        assert "tag" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_tag_rejects_whitespace():
    try:
        helper._resolve_tag("bad tag")
    except ValueError as exc:
        assert "whitespace" in str(exc)
    else:
        raise AssertionError("expected ValueError")
