from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_DEFAULT_ROOTS = ("app", "mcp", "scripts")
_IGNORED_RELATIVE_PATHS = {
    "app/services/code_hardcoding_audit_service.py",
}
_TEXT_EXTENSIONS = {
    ".py",
    ".ps1",
    ".psm1",
    ".md",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".sh",
}

_PATTERN_REGISTRY: list[dict[str, Any]] = [
    {
        "category": "private_network_url",
        "severity": "warning",
        "description": "Hardcoded private-network or localhost URL found in code or scripts.",
        "suggestion": "Move endpoint defaults into config, setup flows, or environment-driven examples.",
        "regex": re.compile(
            r"https?://(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})(?::\d+)?",
            re.IGNORECASE,
        ),
    },
    {
        "category": "hardcoded_local_path",
        "severity": "warning",
        "description": "Hardcoded machine-local filesystem path found in code.",
        "suggestion": "Prefer relative paths, config, setup-time generation, or operator-provided paths.",
        "regex": re.compile(
            r"(?:[A-Za-z]:\\\\[^\"']+|/home/[^\"'\s]+|/Users/[^\"'\s]+|/var/[^\"'\s]+|/tmp/[^\"'\s]+)",
            re.IGNORECASE,
        ),
    },
    {
        "category": "hardcoded_api_key_value",
        "severity": "error",
        "description": "Looks like a concrete API key or shared secret value is embedded in code or scripts.",
        "suggestion": "Remove fixed credentials from code paths and replace them with environment/config injection.",
        "regex": re.compile(
            r"(?i)(?:mnemoforge-local|api[_-]?key\s*[:=]\s*['\"][^'\"]{6,}['\"]|x-api-key['\"]?\s*[:=]\s*['\"][^'\"]{6,}['\"])",
        ),
    },
    {
        "category": "hardcoded_scope_identifier",
        "severity": "warning",
        "description": "A concrete project, slice, or scoped identifier appears hardcoded in code.",
        "suggestion": "Store scoped identifiers in project/integrity data stores or config instead of embedding them in logic.",
        "regex": re.compile(
            r"(?i)(?:project(?:_id)?\s*[:=]\s*['\"][A-Za-z0-9_.-]{6,}['\"]|slice_id\s*[:=]\s*['\"][A-Za-z0-9_.-]{6,}['\"]|qdrant\.skill_domain_tags_filter|linuxcnc-gipric3a)",
        ),
    },
]


def _iter_candidate_files(repo_root: Path, roots: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root_name in roots:
        root = repo_root / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _TEXT_EXTENSIONS:
                continue
            rel = path.relative_to(repo_root).as_posix()
            if rel in _IGNORED_RELATIVE_PATHS:
                continue
            files.append(path)
    return files


def _should_suppress(*, category: str, line: str, matched_text: str) -> bool:
    stripped = line.strip()
    line_upper = line.upper()
    matched_upper = matched_text.upper()
    if stripped.startswith("#") and ("EXAMPLE:" in line_upper or "E.G." in line_upper):
        return True
    if category == "hardcoded_api_key_value":
        if "__API_KEY__" in line_upper or "__TOKEN__" in line_upper or "__SECRET__" in line_upper:
            return True
        if matched_upper.startswith("API_KEY") and "__" in line_upper:
            return True
    if category == "hardcoded_scope_identifier":
        if re.match(r"^[A-Z0-9_]+\s*=\s*[\"'][A-Za-z0-9_.-]{6,}[\"']", stripped):
            return True
    return False


def run_code_hardcoding_audit(
    *,
    repo_root: str | Path | None = None,
    roots: tuple[str, ...] = _DEFAULT_ROOTS,
    limit_per_category: int = 100,
) -> dict[str, Any]:
    base = Path(repo_root or Path.cwd()).resolve()
    findings: list[dict[str, Any]] = []
    by_category: dict[str, int] = {}

    for path in _iter_candidate_files(base, roots):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        rel = path.relative_to(base).as_posix()
        for lineno, line in enumerate(lines, 1):
            for spec in _PATTERN_REGISTRY:
                category = str(spec["category"])
                current = by_category.get(category, 0)
                if current >= limit_per_category:
                    continue
                match = spec["regex"].search(line)
                if not match:
                    continue
                matched_text = match.group(0)[:120]
                if _should_suppress(category=category, line=line, matched_text=matched_text):
                    continue
                findings.append(
                    {
                        "category": category,
                        "severity": spec["severity"],
                        "description": spec["description"],
                        "suggestion": spec["suggestion"],
                        "file_path": rel,
                        "line_number": lineno,
                        "excerpt": line.strip()[:240],
                        "matched_text": matched_text,
                    }
                )
                by_category[category] = current + 1

    findings.sort(key=lambda item: (item["severity"] != "error", item["category"], item["file_path"], item["line_number"]))
    next_actions: list[str] = []
    if by_category.get("hardcoded_api_key_value"):
        next_actions.append("Remove concrete API key values from tracked code before any public release.")
    if by_category.get("private_network_url") or by_category.get("hardcoded_local_path"):
        next_actions.append("Replace machine-specific endpoints and local paths with config, setup-time injection, or generated examples.")
    if by_category.get("hardcoded_scope_identifier"):
        next_actions.append("Audit hardcoded scoped identifiers and move real specifics into project or integrity stores.")

    return {
        "status": "warning" if findings else "ok",
        "roots": list(roots),
        "total_findings": len(findings),
        "by_category": by_category,
        "findings": findings,
        "next_actions": next_actions,
    }
