from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.services.functionality_inventory_service import build_functionality_alpha_config


def _looks_like_project_root(path: Path) -> bool:
    return (
        (path / "README.md").exists()
        and (path / "app").exists()
        and (path / "app" / "services").exists()
    )


def _resolve_project_root() -> Path:
    env_override = str(os.getenv("SUPERMEMORY_PROJECT_ROOT", "")).strip()
    if env_override:
        candidate = Path(env_override).expanduser().resolve()
        if _looks_like_project_root(candidate):
            return candidate

    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if _looks_like_project_root(candidate):
            return candidate

    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        if _looks_like_project_root(candidate):
            return candidate

    return here.parents[2]


_PROJECT_ROOT = _resolve_project_root()
_CRITICAL_IGNORE_RULES = [
    ".env",
    ".venv/",
    "qdrant_data/",
    "logs/",
    "*.db",
    ".server.pid",
]
_ENV_REQUIRED_KEYS = [
    "SELF_PROJECT_ID",
    "DISABLED_MODULES",
    "API_KEY",
]
_PUBLIC_DOC_FILES = {
    "readme": "README.md",
    "setup": "SETUP.md",
    "client_setup": "CLIENT_SETUP.md",
    "architecture": "docs/PROJECT_KNOWLEDGE_MODEL.md",
    "roadmap": "docs/EXTERNAL_PROJECT_ROADMAP.md",
    "status_doc": "STATUS.md",
}
_DEMO_FILES = {
    "demo_readme": "demo/README.md",
    "demo_memories": "demo/demo_memories.jsonl",
}
_MOJIBAKE_MARKERS = ("Ð", "Ñ", "╨", "╩", "╬", "тАЭ")
_LOCAL_PATH_RE = re.compile(r"([A-Za-z]:\\[^ \n\r\t`]+|/(?:home|Users|var|tmp)/[^ \n\r\t`]+)")
_PRIVATE_IP_RE = re.compile(
    r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b"
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _check_quickstart(readme_text: str) -> bool:
    lower = readme_text.lower()
    return "quick start" in lower or "quickstart" in lower


def _find_text_issues(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "present": False,
            "file": path.relative_to(_PROJECT_ROOT).as_posix(),
            "mojibake_markers": 0,
            "local_path_matches": [],
            "private_network_matches": [],
        }
    text = _read_text(path)
    mojibake_markers = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    local_paths = sorted(set(match.group(0) for match in _LOCAL_PATH_RE.finditer(text)))
    private_ips = sorted(set(match.group(0) for match in _PRIVATE_IP_RE.finditer(text)))
    return {
        "present": True,
        "file": path.relative_to(_PROJECT_ROOT).as_posix(),
        "mojibake_markers": mojibake_markers,
        "local_path_matches": local_paths[:20],
        "private_network_matches": private_ips[:20],
    }


def _package_presence() -> dict[str, Any]:
    files = {
        "readme": (_PROJECT_ROOT / "README.md").exists(),
        "setup": (_PROJECT_ROOT / "SETUP.md").exists(),
        "client_setup": (_PROJECT_ROOT / "CLIENT_SETUP.md").exists(),
        "dockerfile": (_PROJECT_ROOT / "Dockerfile").exists(),
        "docker_compose": (_PROJECT_ROOT / "docker-compose.yml").exists(),
        "env_example": (_PROJECT_ROOT / ".env.example").exists(),
    }
    return {
        "files": files,
        "missing": [name for name, present in files.items() if not present],
    }


def _env_example_audit() -> dict[str, Any]:
    path = _PROJECT_ROOT / ".env.example"
    if not path.exists():
        return {
            "present": False,
            "keys_present": [],
            "missing_keys": list(_ENV_REQUIRED_KEYS),
        }
    text = _read_text(path)
    keys_present = []
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        keys_present.append(key)
        values[key] = value.strip()
    keys_present = sorted(set(keys_present))
    missing_keys = [key for key in _ENV_REQUIRED_KEYS if key not in keys_present]
    return {
        "present": True,
        "keys_present": keys_present,
        "values": values,
        "missing_keys": missing_keys,
    }


def _public_docs_coverage() -> dict[str, Any]:
    readme_path = _PROJECT_ROOT / "README.md"
    readme_present = readme_path.exists()
    readme_text = _read_text(readme_path) if readme_present else ""
    items = {
        name: {
            "present": (_PROJECT_ROOT / rel_path).exists(),
            "file": rel_path,
        }
        for name, rel_path in _PUBLIC_DOC_FILES.items()
    }
    items["quickstart"] = {
        "present": readme_present and _check_quickstart(readme_text),
        "file": "README.md",
    }
    return {
        "items": items,
        "missing": [name for name, item in items.items() if not item["present"]],
    }


def _demo_dataset_presence() -> dict[str, Any]:
    items = {
        name: {
            "present": (_PROJECT_ROOT / rel_path).exists(),
            "file": rel_path,
        }
        for name, rel_path in _DEMO_FILES.items()
    }
    return {
        "items": items,
        "missing": [name for name, item in items.items() if not item["present"]],
    }


def _sanitization_audit() -> dict[str, Any]:
    gitignore_path = _PROJECT_ROOT / ".gitignore"
    gitignore_text = _read_text(gitignore_path) if gitignore_path.exists() else ""
    missing_ignore_rules = [rule for rule in _CRITICAL_IGNORE_RULES if rule not in gitignore_text]

    docs_audit = {
        name: _find_text_issues(_PROJECT_ROOT / rel_path)
        for name, rel_path in _PUBLIC_DOC_FILES.items()
        if name != "status_doc"
    }
    mojibake_docs = [
        item["file"]
        for item in docs_audit.values()
        if item["present"] and item["mojibake_markers"] > 0
    ]
    local_path_docs = [
        item["file"]
        for item in docs_audit.values()
        if item["present"] and item["local_path_matches"]
    ]
    private_network_docs = [
        item["file"]
        for item in docs_audit.values()
        if item["present"] and item["private_network_matches"]
    ]
    return {
        "gitignore_present": gitignore_path.exists(),
        "critical_ignore_rules_present": [rule for rule in _CRITICAL_IGNORE_RULES if rule not in missing_ignore_rules],
        "missing_ignore_rules": missing_ignore_rules,
        "docs_audit": docs_audit,
        "issues": {
            "mojibake_docs": mojibake_docs,
            "local_path_docs": local_path_docs,
            "private_network_docs": private_network_docs,
        },
    }


def build_publish_readiness() -> dict[str, Any]:
    package_presence = _package_presence()
    public_docs = _public_docs_coverage()
    demo_dataset = _demo_dataset_presence()
    sanitization = _sanitization_audit()
    env_example = _env_example_audit()
    alpha_surface = build_functionality_alpha_config()

    blockers: list[str] = []
    warnings: list[str] = []

    if package_presence["missing"]:
        blockers.append(f"Missing packaging files: {', '.join(package_presence['missing'])}")
    if public_docs["missing"]:
        blockers.append(f"Missing public docs coverage: {', '.join(public_docs['missing'])}")
    if demo_dataset["missing"]:
        blockers.append(f"Missing safe demo dataset assets: {', '.join(demo_dataset['missing'])}")
    if sanitization["missing_ignore_rules"]:
        blockers.append(f"Missing critical .gitignore rules: {', '.join(sanitization['missing_ignore_rules'])}")
    if env_example["missing_keys"]:
        blockers.append(f"Missing public-alpha env defaults: {', '.join(env_example['missing_keys'])}")

    issues = sanitization["issues"]
    if issues["mojibake_docs"]:
        warnings.append(f"Mojibake detected in public docs: {', '.join(issues['mojibake_docs'])}")
    if issues["local_path_docs"]:
        warnings.append(f"Local absolute paths detected in public docs: {', '.join(issues['local_path_docs'])}")
    if issues["private_network_docs"]:
        warnings.append(f"Private network IP examples detected in public docs: {', '.join(issues['private_network_docs'])}")
    configured_disabled_modules = env_example.get("values", {}).get("DISABLED_MODULES", "")
    recommended_disabled_modules = alpha_surface.get("disabled_modules_env", "")
    if alpha_surface.get("disabled_modules") and configured_disabled_modules != recommended_disabled_modules:
        warnings.append(
            "Public alpha should ship with experimental modules disabled by default: "
            + recommended_disabled_modules
        )

    next_actions: list[str] = []
    if not public_docs["items"]["status_doc"]["present"]:
        next_actions.append("Create a dedicated public status document or equivalent alpha-status surface.")
    if issues["mojibake_docs"] or issues["local_path_docs"] or issues["private_network_docs"]:
        next_actions.append("Clean mojibake and machine-local examples from setup/client-facing docs before GitHub release.")
    if alpha_surface.get("disabled_modules") and configured_disabled_modules != recommended_disabled_modules:
        next_actions.append("Publish GitHub alpha with the recommended disabled experimental modules baseline.")
    if demo_dataset["missing"]:
        next_actions.append("Prepare a safe demo dataset instead of shipping any live service data.")

    return {
        "status": "warning" if blockers or warnings else "ok",
        "publish_target": "github_alpha",
        "readiness_version": 1,
        "package_presence": package_presence,
        "public_docs": public_docs,
        "demo_dataset": demo_dataset,
        "env_example": env_example,
        "sanitization": sanitization,
        "alpha_surface": alpha_surface,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": next_actions,
    }
