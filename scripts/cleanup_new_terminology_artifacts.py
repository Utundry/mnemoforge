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

from app.services.new_terminology import sanitize_new_terminology_candidate


TERM_RE = re.compile(r"new terminology requiring normalization: '([^']+)'", re.IGNORECASE)


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


def extract_term(observation: str) -> str:
    match = TERM_RE.search(observation or "")
    return match.group(1).strip() if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive non-reviewable new terminology artifacts.")
    parser.add_argument("--db", default=str(Path("qdrant_data") / "learning.db"))
    parser.add_argument("--apply", action="store_true", help="Apply archive changes")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, observation, meta_json, status
        FROM artifacts
        WHERE artifact_type = 'meta_guidance'
          AND status = 'pending_review'
        ORDER BY created_at DESC
        """
    ).fetchall()

    matches: list[tuple[str, str]] = []
    now = time.time()
    for row in rows:
        meta = _load_json(row["meta_json"])
        if str(meta.get("signal_type") or "") != "new_terminology":
            continue
        term = extract_term(str(row["observation"] or ""))
        if not term:
            continue
        if sanitize_new_terminology_candidate(term) is not None:
            continue
        matches.append((str(row["id"]), term))
        if args.apply:
            cleanup = meta.get("cleanup", {})
            cleanup.update({
                "cleaned_at": now,
                "cleaned_by": "cleanup_new_terminology_artifacts",
                "reason": "known_infrastructure_term_not_glossary_candidate",
                "term": term,
            })
            meta["cleanup"] = cleanup
            cur.execute(
                "UPDATE artifacts SET status = 'archived', updated_at = ?, meta_json = ? WHERE id = ?",
                (now, _dump_json(meta), str(row["id"])),
            )

    if args.apply:
        conn.commit()
    conn.close()

    print(f"matched={len(matches)}")
    for artifact_id, term in matches:
        print(f"{artifact_id}\t{term}")
    if args.apply:
        print("applied=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
