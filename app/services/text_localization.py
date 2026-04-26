from __future__ import annotations

import json
import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

_MOJIBAKE_MARKERS = (
    "Р", "С", "Ð", "Ñ", "â", "Â", "Ã", "�",
)
_MOJIBAKE_RE = re.compile(r"(?:Р.|С.|Ð.|Ñ.|â.|Â.|Ã.){2,}|�")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")

_TRANSLATE_FIELDS_PROMPT = """/no_think
Translate these artifact fields to {language}.

Rules:
- Preserve file paths, commands, code identifiers, API names, and technical acronyms.
- Keep meaning precise and concise.
- Do not add explanations.
- Return only valid JSON with exactly these keys:
  "content", "observation", "why_it_matters"

JSON:
{payload}
"""


def looks_like_mojibake(text: str) -> bool:
    return bool(text and _MOJIBAKE_RE.search(text))


def _mojibake_marker_count(text: str) -> int:
    return sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)


def _text_quality_score(text: str) -> int:
    if not text:
        return -1000
    cyr = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    bad = len(_MOJIBAKE_RE.findall(text))
    marker_hits = _mojibake_marker_count(text)
    replacement = text.count("�")
    return cyr * 2 + latin - marker_hits * 6 - bad * 14 - replacement * 10


def _try_redecode(text: str, source_encoding: str) -> str | None:
    try:
        return text.encode(source_encoding, errors="strict").decode("utf-8", errors="strict")
    except Exception:
        return None


def repair_mojibake(text: str) -> str:
    if not looks_like_mojibake(text):
        return text

    best = text
    best_score = _text_quality_score(text)
    best_markers = _mojibake_marker_count(text)
    for encoding in ("cp1251", "latin-1", "windows-1252"):
        candidate = _try_redecode(text, encoding)
        if not candidate:
            continue
        marker_hits = _mojibake_marker_count(candidate)
        score = _text_quality_score(candidate)
        if (marker_hits < best_markers and score >= best_score) or score > best_score + 4:
            best = candidate
            best_score = score
            best_markers = marker_hits
    return best


def normalize_text_for_display(text: str) -> str:
    normalized = (text or "").strip()
    # Some historical artifacts were re-encoded multiple times by shell hops.
    # Run a bounded repair loop so second-order mojibake is also recovered.
    for _ in range(3):
        repaired = repair_mojibake(normalized)
        if repaired == normalized:
            break
        normalized = repaired.strip()
        if not looks_like_mojibake(normalized):
            break
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    normalized = "\n".join(
        re.sub(r"[ \u00a0]{2,}", " ", line).strip()
        for line in normalized.split("\n")
    )
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized


def is_low_quality_text(text: str, *, min_score: int = 6, max_mojibake_markers: int = 1) -> bool:
    normalized = normalize_text_for_display(text or "")
    if not normalized:
        return True
    marker_count = _mojibake_marker_count(normalized)
    if marker_count > max_mojibake_markers:
        return True
    # Short, clean technical labels are often valid despite carrying little language signal.
    if len(normalized) <= 24 and marker_count == 0 and not looks_like_mojibake(normalized):
        return False
    return _text_quality_score(normalized) < min_score


def _should_translate(target_language: str, fields: dict[str, str]) -> bool:
    language = (target_language or "").strip().lower()
    if not language or language in {"english", "en", "eng"}:
        return False

    combined = " ".join(v for v in fields.values() if v).strip()
    if not combined:
        return False

    cyr = len(_CYRILLIC_RE.findall(combined))
    lat = len(_LATIN_RE.findall(combined))
    if lat >= 20:
        return True
    return lat > 0 and cyr < max(6, lat // 3)


async def _translate_fields(fields: dict[str, str], *, language: str) -> dict[str, str] | None:
    from app.services.cloud_llm import cloud_available
    from app.services.llm_gateway import get_cloud_gateway

    if not cloud_available():
        return None

    payload = json.dumps(fields, ensure_ascii=False)
    prompt = _TRANSLATE_FIELDS_PROMPT.format(language=language, payload=payload)
    try:
        raw = await get_cloud_gateway().generate(
            prompt,
            system=(
                f"You are a translator. Translate to {language}. "
                "Keep commands, code, paths, and technical identifiers unchanged."
            ),
            task_type="text_summarization",
            mode="economy",
            max_tokens=450,
            temperature=0.1,
            allow_local_fallback=False,
        )
    except Exception as exc:
        logger.warning("Artifact translation failed: %s", exc)
        return None

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except Exception as exc:
        logger.warning("Artifact translation JSON parse failed: %s", exc)
        return None

    translated: dict[str, str] = {}
    for key in ("content", "observation", "why_it_matters"):
        translated[key] = normalize_text_for_display(str(data.get(key, fields.get(key, ""))))
    return translated


async def prepare_artifact_texts(
    *,
    content: str,
    observation: str = "",
    why_it_matters: str = "",
    meta: dict | None = None,
) -> tuple[dict[str, str], dict]:
    cleaned = {
        "content": normalize_text_for_display(content),
        "observation": normalize_text_for_display(observation),
        "why_it_matters": normalize_text_for_display(why_it_matters),
    }
    return cleaned, dict(meta or {})


async def translate_artifact_fields(
    *,
    content: str,
    observation: str = "",
    why_it_matters: str = "",
    target_language: str | None = None,
) -> dict[str, str]:
    cleaned = {
        "content": normalize_text_for_display(content),
        "observation": normalize_text_for_display(observation),
        "why_it_matters": normalize_text_for_display(why_it_matters),
    }
    language = (target_language or settings.glm_response_language or "").strip()
    translated = await _translate_fields(cleaned, language=language) if _should_translate(language, cleaned) else None
    return {
        "language": language or "English",
        "original_content": cleaned["content"],
        "original_observation": cleaned["observation"],
        "original_why_it_matters": cleaned["why_it_matters"],
        "translated_content": (translated or cleaned)["content"],
        "translated_observation": (translated or cleaned)["observation"],
        "translated_why_it_matters": (translated or cleaned)["why_it_matters"],
    }


def hydrate_artifact_display_fields(row: dict, *, target_language: str | None = None) -> dict:
    hydrated = dict(row)
    language = (target_language or settings.glm_response_language or "").strip()
    hydrated["display_language"] = language or "English"
    hydrated["display_content"] = normalize_text_for_display(hydrated.get("content", ""))
    hydrated["display_observation"] = normalize_text_for_display(hydrated.get("observation", ""))
    hydrated["display_why_it_matters"] = normalize_text_for_display(hydrated.get("why_it_matters", ""))
    return hydrated
