from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from scripts.public_release_config import validate_public_env


REQUIRED_DOCKERIGNORE_RULES = [
    ".git/",
    ".*",
    ".env",
    ".venv/",
    "node_modules/",
    "qdrant_data/",
    "logs/",
    "*.db",
    "pytest_temp_*/",
    "tests/",
    "pytest.ini",
]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _validate_dockerignore(project_root: Path) -> list[str]:
    path = project_root / ".dockerignore"
    if not path.exists():
        return ["Missing .dockerignore"]
    text = _read_text(path)
    return [rule for rule in REQUIRED_DOCKERIGNORE_RULES if rule not in text]


def _resolve_repository(value: str | None) -> str:
    repository = str(value or os.environ.get("DOCKERHUB_REPOSITORY") or os.environ.get("DOCKER_IMAGE_REPOSITORY") or "").strip()
    if not repository:
        raise ValueError("Docker Hub repository is required. Pass --repository or set DOCKERHUB_REPOSITORY.")
    if repository.startswith("http://") or repository.startswith("https://"):
        raise ValueError("Repository must be a Docker image name, not a URL.")
    if ":" in repository:
        raise ValueError("Repository must not include a tag; pass the tag separately with --tag.")
    if "/" not in repository:
        raise ValueError("Repository should look like namespace/name for Docker Hub.")
    return repository


def _resolve_tag(value: str | None) -> str:
    tag = str(value or os.environ.get("DOCKER_IMAGE_TAG") or "latest").strip()
    if not tag:
        raise ValueError("Docker image tag cannot be empty.")
    if any(ch.isspace() for ch in tag):
        raise ValueError("Docker image tag cannot contain whitespace.")
    return tag


def _resolve_current_git_sha(project_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=False,
        cwd=str(project_root),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "git rev-parse failed").strip()
        raise RuntimeError(f"Cannot resolve current git SHA: {message}")
    sha = completed.stdout.strip()
    if not sha:
        raise RuntimeError("Cannot resolve current git SHA: empty output")
    return sha


def _run(cmd: list[str], *, dry_run: bool) -> int:
    print("$ " + " ".join(cmd))
    if dry_run:
        return 0
    completed = subprocess.run(cmd, check=False)
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and optionally push a public Docker Hub image.")
    parser.add_argument("--repository", help="Docker Hub repository, for example user/mnemoforge.")
    parser.add_argument("--tag", help="Image tag to publish. Defaults to latest.")
    parser.add_argument("--context", default=".", help="Build context directory.")
    parser.add_argument("--dockerfile", default="Dockerfile", help="Path to Dockerfile.")
    parser.add_argument("--push", action="store_true", help="Push the image after a successful build.")
    parser.add_argument(
        "--tag-current-git-sha",
        action="store_true",
        help="Also tag the built image as repository:<current-git-sha> and push it when --push is set.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument("--check", action="store_true", help="Validate release files and exit without building.")
    parser.add_argument("--template", default=".env.public.example", help="Public env template to validate.")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    template_path = project_root / args.template
    if not template_path.exists():
        print(f"Missing public env template: {template_path}", file=sys.stderr)
        return 2

    public_env_report = validate_public_env(_read_text(template_path))
    dockerignore_missing = _validate_dockerignore(project_root)
    if public_env_report["missing_required"] or public_env_report["forbidden_present"] or dockerignore_missing:
        print("Public release checks failed:", file=sys.stderr)
        if public_env_report["missing_required"]:
            print(f"  missing_required: {', '.join(public_env_report['missing_required'])}", file=sys.stderr)
        if public_env_report["forbidden_present"]:
            print(f"  forbidden_present: {', '.join(public_env_report['forbidden_present'])}", file=sys.stderr)
        if dockerignore_missing:
            print(f"  dockerignore_missing: {', '.join(dockerignore_missing)}", file=sys.stderr)
        return 1

    repository = _resolve_repository(args.repository)
    tag = _resolve_tag(args.tag)
    image_ref = f"{repository}:{tag}"
    git_sha = ""
    if args.tag_current_git_sha:
        try:
            git_sha = _resolve_current_git_sha(project_root)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.check:
        print(f"Public release checks passed for {image_ref}")
        return 0

    build_cmd = [
        "docker",
        "build",
        "-f",
        str(args.dockerfile),
        "-t",
        image_ref,
        "--build-arg",
        f"MNEMOFORGE_GIT_COMMIT={git_sha or 'unknown'}",
        "--build-arg",
        f"MNEMOFORGE_BUILD_TAG={tag}",
        "--build-arg",
        f"MNEMOFORGE_IMAGE_REPOSITORY={repository}",
        str(args.context),
    ]
    rc = _run(build_cmd, dry_run=args.dry_run)
    if rc != 0:
        return rc

    if args.push:
        push_cmd = ["docker", "push", image_ref]
        rc = _run(push_cmd, dry_run=args.dry_run)
        if rc != 0:
            return rc

    immutable_ref = ""
    if args.tag_current_git_sha:
        immutable_ref = f"{repository}:{git_sha}"
        tag_cmd = ["docker", "tag", image_ref, immutable_ref]
        rc = _run(tag_cmd, dry_run=args.dry_run)
        if rc != 0:
            return rc
        if args.push:
            push_cmd = ["docker", "push", immutable_ref]
            rc = _run(push_cmd, dry_run=args.dry_run)
            if rc != 0:
                return rc

    print(f"Ready: {image_ref}")
    if immutable_ref:
        print(f"Immutable: {immutable_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
