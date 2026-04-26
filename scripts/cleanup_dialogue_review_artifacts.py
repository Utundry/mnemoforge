from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.new_terminology import (
    sanitize_new_terminology_candidate,
    sanitize_successful_pattern_candidate,
)
from app.services.text_localization import is_low_quality_text, normalize_text_for_display

TERM_RE = re.compile(r"new terminology requiring normalization: '([^']+)'", re.IGNORECASE)
PATTERN_RE = re.compile(r"reusable successful pattern: '([^']+)'", re.IGNORECASE)
SKILL_GAP_RE = re.compile(r"skill gap for '([^']+)'", re.IGNORECASE)


def _load_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _dump_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _extract(regex: re.Pattern[str], text: str) -> str:
    match = regex.search(text or "")
    return normalize_text_for_display(match.group(1)) if match else ""


def _looks_like_dialogue_excerpt(text: str) -> bool:
    normalized = normalize_text_for_display(text)
    if not normalized:
        return False
    return "USER:" in normalized and "ASSISTANT:" in normalized


def _archive_reason(row: sqlite3.Row) -> tuple[bool, str]:
    meta = _load_json(row["meta_json"])
    signal_type = str(meta.get("signal_type") or "")
    observation = str(row["observation"] or "")
    content = str(row["content"] or "")
    transcript_hint = normalize_text_for_display(
        str(meta.get("dialogue_excerpt") or observation or content)
    )

    if is_low_quality_text(content):
        return True, "low_quality_dialogue_artifact"
    if is_low_quality_text(transcript_hint):
        return True, "low_quality_dialogue_artifact"

    if signal_type == "new_terminology":
        term = _extract(TERM_RE, observation) or content
        if sanitize_new_terminology_candidate(term, transcript=transcript_hint) is None:
            return True, "non_glossary_term_in_procedural_context"
    elif signal_type == "successful_pattern":
        pattern = _extract(PATTERN_RE, observation) or content
        if sanitize_successful_pattern_candidate(pattern, transcript=transcript_hint) is None:
            return True, "pattern_without_success_evidence"
    elif signal_type == "skill_gap":
        subject = _extract(SKILL_GAP_RE, observation)
        if is_low_quality_text(transcript_hint):
            return True, "low_quality_skill_gap_signal"
        if not _looks_like_dialogue_excerpt(transcript_hint):
            return True, "non_dialogue_excerpt"
        if not subject:
            return True, "low_quality_skill_gap_signal"
    elif not _looks_like_dialogue_excerpt(transcript_hint):
        return True, "non_dialogue_excerpt"

    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive non-reviewable pending dialogue artifacts.")
    parser.add_argument("--db", default=str(Path("qdrant_data") / "learning.db"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, status, content, observation, meta_json
        FROM artifacts
        WHERE artifact_type = 'meta_guidance'
          AND status = 'pending_review'
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()

    now = time.time()
    actions: list[dict[str, str]] = []
    for row in rows:
        should_archive, reason = _archive_reason(row)
        if not should_archive:
            continue
        actions.append({"id": str(row["id"]), "reason": reason})
        if not args.apply:
            continue
        meta = _load_json(row["meta_json"])
        cleanup = dict(meta.get("cleanup") or {})
        cleanup.update({
            "cleaned_at": now,
            "cleaned_by": "cleanup_dialogue_review_artifacts",
            "reason": reason,
        })
        meta["cleanup"] = cleanup
        cur.execute(
            """
            UPDATE artifacts
            SET status = 'archived', updated_at = ?, meta_json = ?
            WHERE id = ?
            """,
            (now, _dump_json(meta), str(row["id"])),
        )

    if args.apply:
        conn.commit()
    conn.close()

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "matched": len(actions),
        "actions": actions,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
