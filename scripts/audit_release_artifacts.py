from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path


FORBIDDEN_CONTEXT_PATHS = (
    ".env",
    ".git/",
    ".venv/",
    ".venv-wsl/",
    "system_data/",
    "qdrant_data/",
    "logs/",
    "tests/",
    "tmp_test_code_search/",
    "pytest_temp_",
)
FORBIDDEN_IMAGE_PATHS = (
    "/app/.env",
    "/app/.git",
    "/app/.venv",
    "/app/.venv-wsl",
    "/app/system_data",
    "/app/logs",
    "/app/tests",
    "/app/tmp_test_code_search",
    "/app/pytest.ini",
    "/app/scripts",
    "/app/docs/PROJECT_LAW.md",
)
FORBIDDEN_NAME_FRAGMENTS = (
    "api_key",
    "secret",
    "token",
    "credential",
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _dockerignore_patterns(project_root: Path) -> list[str]:
    path = project_root / ".dockerignore"
    if not path.exists():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _ignored_by_patterns(rel_path: str, patterns: list[str]) -> bool:
    normalized = rel_path.replace("\\", "/")
    parts = normalized.split("/")
    ignored = False
    for pattern in patterns:
        clean = pattern.strip().replace("\\", "/").lstrip("/")
        if not clean:
            continue
        is_negation = pattern.strip().startswith("!")
        if is_negation:
            clean = clean.lstrip("!")
        matched = False
        if clean.endswith("/"):
            prefix = clean.rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/") or prefix in parts:
                matched = True
        elif "/" not in clean and any(fnmatch.fnmatch(part, clean) for part in parts):
            matched = True
        elif fnmatch.fnmatch(normalized, clean) or normalized.startswith(clean.rstrip("/") + "/"):
            matched = True
        if matched:
            ignored = not is_negation
    return ignored


def _docker_context_files(project_root: Path) -> list[str]:
    patterns = _dockerignore_patterns(project_root)
    files = []
    for root, dirs, names in os.walk(project_root):
        root_path = Path(root)
        rel_root = root_path.relative_to(project_root).as_posix()
        if rel_root == ".":
            rel_root = ""
        kept_dirs = []
        for dirname in dirs:
            rel_dir = f"{rel_root}/{dirname}".strip("/")
            if not _ignored_by_patterns(rel_dir + "/", patterns):
                kept_dirs.append(dirname)
        dirs[:] = kept_dirs
        for name in names:
            rel = f"{rel_root}/{name}".strip("/")
            if _ignored_by_patterns(rel, patterns):
                continue
            files.append(rel)
    return sorted(files)


def _image_files(image: str) -> list[str]:
    completed = _run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c", "import os, json; print(json.dumps([os.path.join(r, f) for r, _, fs in os.walk('/app') for f in fs]))"])
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return sorted(str(item) for item in json.loads(completed.stdout))


def _matches_forbidden(paths: list[str], forbidden: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        lower = normalized.lower()
        for rule in forbidden:
            rule_raw = rule.lower()
            rule_lower = rule_raw.rstrip("/")
            if rule_lower == ".env":
                if Path(lower).name == ".env":
                    matches.append(normalized)
                    break
                continue
            if rule_lower.startswith("/"):
                if lower == rule_lower or lower.startswith(rule_lower + "/"):
                    matches.append(normalized)
                    break
                continue
            if rule_raw.endswith("/"):
                if (
                    lower == rule_lower
                    or lower.startswith(rule_lower + "/")
                    or f"/{rule_lower}/" in lower
                ):
                    matches.append(normalized)
                    break
            elif rule_lower in lower:
                matches.append(normalized)
                break
    return sorted(set(matches))


def audit_context(project_root: Path) -> dict[str, object]:
    files = _docker_context_files(project_root)
    suspicious_names = [
        path
        for path in files
        if any(fragment in Path(path).name.lower() for fragment in FORBIDDEN_NAME_FRAGMENTS)
    ]
    forbidden = _matches_forbidden(files, FORBIDDEN_CONTEXT_PATHS)
    return {
        "kind": "docker_context",
        "file_count": len(files),
        "forbidden_matches": forbidden[:50],
        "suspicious_names": suspicious_names[:50],
        "ok": not forbidden and not suspicious_names,
    }


def audit_image(image: str) -> dict[str, object]:
    files = _image_files(image)
    suspicious_names = [
        path
        for path in files
        if any(fragment in Path(path).name.lower() for fragment in FORBIDDEN_NAME_FRAGMENTS)
    ]
    forbidden = _matches_forbidden(files, FORBIDDEN_IMAGE_PATHS)
    return {
        "kind": "docker_image",
        "image": image,
        "file_count": len(files),
        "forbidden_matches": forbidden[:50],
        "suspicious_names": suspicious_names[:50],
        "ok": not forbidden and not suspicious_names,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit public release Docker context and image contents.")
    parser.add_argument("--project-root", default=".", help="Project root containing Dockerfile and .dockerignore.")
    parser.add_argument("--image", help="Optional image reference to inspect.")
    parser.add_argument("--skip-context", action="store_true", help="Skip build-context audit.")
    args = parser.parse_args()

    reports = []
    try:
        if not args.skip_context:
            reports.append(audit_context(Path(args.project_root).resolve()))
        if args.image:
            reports.append(audit_image(args.image))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    ok = all(bool(report.get("ok")) for report in reports)
    print(json.dumps({"ok": ok, "reports": reports}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
