from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PUBLIC_TEMPLATE_NAME = ".env.public.example"
PUBLIC_SAFE_KEYS = {
    "QDRANT_HOST",
    "QDRANT_PORT",
    "QDRANT_IN_MEMORY",
    "QDRANT_COLLECTION_NAME",
    "OLLAMA_BASE_URL",
    "OLLAMA_EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "SERVER_HOST",
    "SERVER_PORT",
    "LOG_LEVEL",
    "API_PREFIX",
    "SELF_PROJECT_ID",
    "PUBLIC_PROJECT_ALIAS",
    "DISABLED_MODULES",
    "API_KEY",
    "INGEST_ALLOWED_ROOTS",
    "MAX_REQUEST_SIZE_MB",
    "LLM_RATE_LIMIT_PER_MIN",
    "MAX_SEARCH_RESULTS",
    "CLEANUP_MIN_IMPORTANCE",
    "CLEANUP_MAX_AGE_DAYS",
    "INTEGRITY_AUDIT_INTERVAL_MINUTES",
    "INTEGRITY_AUTO_REMEDIATE",
    "DATA_HYGIENE_AUDIT_MINUTES",
    "DATA_HYGIENE_AUTO_TEST_CLEANUP",
    "PACKET_BACKGROUND_SYNC_MINUTES",
    "AUTO_REBUILD_SELF_PROJECT_DOCS_MIN_AGE_MINUTES",
    "PROJECT_TREE_DEDUPE_MINUTES",
    "PROJECT_TREE_DEDUPE_GROUP_LIMIT",
    "LLM_GATEWAY_ENABLE_LOCAL_FALLBACK",
    "LOCAL_GENERATE_MODEL",
    "CLOUD_LLM_PROVIDER",
    "CLOUD_LLM_API_KEY",
    "CLOUD_LLM_MODEL",
    "CLOUD_LLM_BASE_URL",
    "WATCHER_AUTO_START",
    "WATCHER_AGENT_ID",
    "WATCHER_ENABLE_DIALOGUE_ANALYSIS",
}

FORBIDDEN_PUBLIC_KEYS = {
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "AGG1_API_KEY",
    "AGG2_API_KEY",
    "GLM_API_KEY",
    "CLOUD_LLM_MODEL_PROFILES",
    "ECONOMY_CLOUD_LLMS",
    "BALANCED_CLOUD_LLMS",
    "REASONING_CLOUD_LLMS",
    "DISABLED_CLOUD_LLMS",
}


def _parse_env_lines(lines: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    ordered: list[str] = []
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        ordered.append(key)
        values[key] = value.strip()
    return ordered, values


def load_public_template(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    _, values = _parse_env_lines(text.splitlines())
    return values


def render_public_env(
    overrides: dict[str, str] | None = None,
    *,
    template_path: Path | None = None,
) -> str:
    path = template_path or (Path(__file__).resolve().parents[1] / PUBLIC_TEMPLATE_NAME)
    values = load_public_template(path) if path.exists() else {}
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        values[key] = str(value)

    ordered_keys = [key for key in values.keys() if key in PUBLIC_SAFE_KEYS]
    extras = [key for key in values.keys() if key not in PUBLIC_SAFE_KEYS]
    ordered_keys.extend(sorted(extras))

    lines = []
    for key in ordered_keys:
        lines.append(f"{key}={values[key]}")
    return "\n".join(lines).strip() + "\n"


def validate_public_env(text: str) -> dict[str, list[str]]:
    ordered, values = _parse_env_lines(text.splitlines())
    present_keys = set(ordered)
    missing_required = [key for key in ("SELF_PROJECT_ID", "DISABLED_MODULES", "API_KEY") if key not in present_keys]
    forbidden_present = sorted(k for k in present_keys if k in FORBIDDEN_PUBLIC_KEYS)
    internal_defaults_present = sorted(k for k in present_keys if k not in PUBLIC_SAFE_KEYS and k.startswith(("AGG", "GLM_", "OPENAI_", "GEMINI_", "DEEPSEEK_")))
    return {
        "missing_required": missing_required,
        "forbidden_present": forbidden_present,
        "internal_defaults_present": internal_defaults_present,
    }
