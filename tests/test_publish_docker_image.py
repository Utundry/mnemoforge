from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import publish_docker_image as helper


_WORKSPACE = Path("publish_test_workspace")


def _prepare_workspace() -> Path:
    _WORKSPACE.mkdir(exist_ok=True)
    return _WORKSPACE


def _cleanup_workspace() -> None:
    if not _WORKSPACE.exists():
        return
    for child in sorted(_WORKSPACE.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    _WORKSPACE.rmdir()


def test_validate_dockerignore_flags_missing_rules():
    tmp_path = _prepare_workspace()
    (tmp_path / ".dockerignore").write_text(".git/\n.env\n", encoding="utf-8")

    missing = helper._validate_dockerignore(tmp_path)

    assert ".venv/" in missing
    assert "system_data/" in missing
    assert "qdrant_data/" in missing
    assert "logs/" in missing


def test_resolve_repository_prefers_argument_then_env(monkeypatch):
    monkeypatch.delenv("DOCKERHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("DOCKER_IMAGE_REPOSITORY", raising=False)
    monkeypatch.setenv("DOCKERHUB_REPOSITORY", "example/sloplesscode")

    assert helper._resolve_repository(None) == "example/sloplesscode"
    assert helper._resolve_repository("other/sloplesscode") == "other/sloplesscode"


def test_resolve_repository_rejects_tagged_value():
    try:
        helper._resolve_repository("example/sloplesscode:latest")
    except ValueError as exc:
        assert "tag" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_alias_repositories_deduplicates_primary_and_duplicates():
    aliases = helper._resolve_alias_repositories(
        ["caveboy/sloplesscode", "caveboy/mnemoforge", "caveboy/mnemoforge"],
        primary_repository="caveboy/sloplesscode",
    )

    assert aliases == ["caveboy/mnemoforge"]


def test_resolve_tag_rejects_whitespace():
    try:
        helper._resolve_tag("bad tag")
    except ValueError as exc:
        assert "whitespace" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_current_git_sha_uses_project_root(monkeypatch):
    calls = []
    tmp_path = _prepare_workspace()

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


def test_main_can_publish_latest_and_current_git_sha(monkeypatch, capsys):
    tmp_path = _prepare_workspace()
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
            "caveboy/sloplesscode",
            "--alias-repository",
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
                "caveboy/sloplesscode:latest",
                "--build-arg",
                "MNEMOFORGE_GIT_COMMIT=abc1234",
                "--build-arg",
                "MNEMOFORGE_BUILD_TAG=latest",
                "--build-arg",
                "MNEMOFORGE_IMAGE_REPOSITORY=caveboy/sloplesscode",
                ".",
            ],
            False,
        ),
        (
            [
                helper.sys.executable,
                "-m",
                "scripts.audit_first_run",
                "--image",
                "caveboy/sloplesscode:latest",
            ],
            False,
        ),
        (["docker", "push", "caveboy/sloplesscode:latest"], False),
        (["docker", "tag", "caveboy/sloplesscode:latest", "caveboy/mnemoforge:latest"], False),
        (["docker", "push", "caveboy/mnemoforge:latest"], False),
        (["docker", "tag", "caveboy/sloplesscode:latest", "caveboy/sloplesscode:abc1234"], False),
        (["docker", "push", "caveboy/sloplesscode:abc1234"], False),
        (["docker", "tag", "caveboy/sloplesscode:latest", "caveboy/mnemoforge:abc1234"], False),
        (["docker", "push", "caveboy/mnemoforge:abc1234"], False),
    ]
    output = capsys.readouterr().out
    assert "Alias: caveboy/mnemoforge:latest" in output
    assert "Immutable: caveboy/sloplesscode:abc1234" in output
    assert "Alias immutable: caveboy/mnemoforge:abc1234" in output
