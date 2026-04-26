from __future__ import annotations

import json
import logging
import re

from app.services.cloud_llm import cloud_available, cloud_complete
from app.services.text_localization import normalize_text_for_display

logger = logging.getLogger(__name__)

_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_EN_COMMON_WORDS = frozenset(
    "the a an is are was were be been being have has had do does did will would could should "
    "may might shall can not no yes and or but if in on at to of for with by from up out it "
    "he she we you they i me him her us them his this that these those what who how when where "
    "all any some one two three get set run use make let go see know need want look just like "
    "good well also more about only then there their which into than them here now new".split()
)


def looks_like_english_text(text: str) -> bool:
    cleaned = normalize_text_for_display(text)
    if not cleaned:
        return False
    if len(_CYRILLIC_RE.findall(cleaned)) > 0:
        return False
    words = [w.strip(".,!?;:\"'()[]").lower() for w in cleaned.split()]
    if not words:
        return False
    en_word_count = sum(1 for w in words if w in _EN_COMMON_WORDS)
    latin_count = len(_LATIN_RE.findall(cleaned))
    return (
        en_word_count >= 2
        or (len(words) >= 4 and latin_count >= max(12, len(cleaned) // 3))
        or (latin_count > 0 and len(_CYRILLIC_RE.findall(cleaned)) == 0 and len(words) <= 4)
    )


async def canonicalize_agent_fields_to_english(
    fields: dict[str, str],
    *,
    allow_cloud: bool = True,
) -> dict[str, str]:
    cleaned = {key: normalize_text_for_display(value) for key, value in fields.items()}
    if not cleaned:
        return cleaned
    if all(not value or looks_like_english_text(value) for value in cleaned.values()):
        return cleaned
    if not allow_cloud or not cloud_available():
        return cleaned

    payload = json.dumps(cleaned, ensure_ascii=False)
    prompt = (
        "/no_think\n"
        "Translate these project knowledge fields into concise technical English.\n\n"
        "Rules:\n"
        "- Preserve commands, file paths, API names, code identifiers, product names, and acronyms.\n"
        "- Keep meaning precise and compact.\n"
        "- Do not add explanations.\n"
        "- Return only valid JSON with exactly the same keys.\n\n"
        f"JSON:\n{payload}\n"
    )
    try:
        raw = await cloud_complete(
            prompt,
            system=(
                "You translate project knowledge into concise technical English. "
                "Preserve technical identifiers and return JSON only."
            ),
            max_tokens=500,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("Knowledge canonicalization translation failed: %s", exc)
        return cleaned

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return cleaned
    try:
        data = json.loads(match.group())
    except Exception as exc:
        logger.warning("Knowledge canonicalization JSON parse failed: %s", exc)
        return cleaned

    translated: dict[str, str] = {}
    for key, value in cleaned.items():
        translated[key] = normalize_text_for_display(str(data.get(key, value)))
    return translated
