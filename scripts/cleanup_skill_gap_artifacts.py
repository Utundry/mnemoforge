from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.skill_gap_domains import canonicalize_skill_gap_domain


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive stale skill-gap review artifacts after ontology cleanup.")
    parser.add_argument("--db", default=str(Path("qdrant_data") / "learning.db"))
    parser.add_argument("--apply", action="store_true", help="Apply cleanup updates")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, domain, status, tags, meta_json
        FROM artifacts
        WHERE status = 'pending_review'
          AND meta_json LIKE '%"signal_type": "skill_gap"%'
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()

    canonical_present: set[str] = set()
    canonicalized_rows: list[tuple[sqlite3.Row, str | None]] = []
    for row in rows:
        canonical = canonicalize_skill_gap_domain(str(row["domain"] or ""))
        canonicalized_rows.append((row, canonical))
        if canonical and canonical == str(row["domain"] or ""):
            canonical_present.add(canonical)

    archived = 0
    actions: list[dict[str, str]] = []
    now = time.time()
    for row, canonical in canonicalized_rows:
        raw_domain = str(row["domain"] or "")
        reason = ""
        if not canonical:
            reason = "generic_or_invalid_skill_gap_domain"
        elif canonical != raw_domain and canonical in canonical_present:
            reason = f"superseded_by_canonical_domain:{canonical}"
        else:
            continue

        actions.append({"id": str(row["id"]), "domain": raw_domain, "reason": reason})
        if not args.apply:
            continue

        meta = _load_json(row["meta_json"])
        cleanup = dict(meta.get("cleanup") or {})
        cleanup.update({
            "cleaned_at": now,
            "cleaned_by": "cleanup_skill_gap_artifacts",
            "reason": reason,
            "original_domain": raw_domain,
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
        archived += 1

    if args.apply:
        conn.commit()
    conn.close()

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "rows_considered": len(rows),
        "archived": archived if args.apply else len(actions),
        "actions": actions,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
