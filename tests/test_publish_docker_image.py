from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_resolve_current_git_sha_uses_project_root(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper._resolve_current_git_sha(tmp_path) == "abc1234"
    assert calls == [
        (
            ["git", "rev-parse", "--short", "HEAD"],
            {
                "check": False,
                "cwd": str(tmp_path),
                "capture_output": True,
                "text": True,
            },
        )
    ]


def test_main_can_publish_latest_and_current_git_sha(monkeypatch, tmp_path: Path, capsys):
    (tmp_path / ".env.public.example").write_text(
        "SELF_PROJECT_ID=mnemoforge\nDISABLED_MODULES=layout_fixer\nAPI_KEY=\n",
        encoding="utf-8",
    )
    (tmp_path / ".dockerignore").write_text("\n".join(helper.REQUIRED_DOCKERIGNORE_RULES), encoding="utf-8")
    calls = []

    monkeypatch.setattr(helper.Path, "resolve", lambda self: tmp_path / "scripts" / "publish_docker_image.py")
    monkeypatch.setattr(helper, "_resolve_current_git_sha", lambda project_root: "abc1234")
    monkeypatch.setattr(helper, "_run", lambda cmd, *, dry_run: calls.append((cmd, dry_run)) or 0)
    monkeypatch.setattr(
        helper.sys,
        "argv",
        [
            "publish_docker_image.py",
            "--repository",
            "caveboy/mnemoforge",
            "--tag",
            "latest",
            "--push",
            "--tag-current-git-sha",
        ],
    )

    assert helper.main() == 0
    assert calls == [
        (
            [
                "docker",
                "build",
                "-f",
                "Dockerfile",
                "-t",
                "caveboy/mnemoforge:latest",
                "--build-arg",
                "MNEMOFORGE_GIT_COMMIT=abc1234",
                "--build-arg",
                "MNEMOFORGE_BUILD_TAG=latest",
                "--build-arg",
                "MNEMOFORGE_IMAGE_REPOSITORY=caveboy/mnemoforge",
                ".",
            ],
            False,
        ),
        (["docker", "push", "caveboy/mnemoforge:latest"], False),
        (["docker", "tag", "caveboy/mnemoforge:latest", "caveboy/mnemoforge:abc1234"], False),
        (["docker", "push", "caveboy/mnemoforge:abc1234"], False),
    ]
    assert "Immutable: caveboy/mnemoforge:abc1234" in capsys.readouterr().out
