from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.skill_gap_domains import canonicalize_skill_gap_title


def normalize_title(title: str) -> str:
    text = title.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonicalize and merge skill-gap improvements.")
    parser.add_argument("--db", default=str(Path("qdrant_data") / "improvements.db"))
    parser.add_argument("--apply", action="store_true", help="Apply updates and merges")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT *
        FROM improvements
        WHERE status = 'open'
          AND lower(title) LIKE 'skill gap detected:%'
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    flagged_rows: list[sqlite3.Row] = []
    renamed = 0
    flagged = 0
    for row in rows:
        canonical_title = canonicalize_skill_gap_title(row["title"])
        if not canonical_title:
            flagged += 1
            flagged_rows.append(row)
            continue
        if canonical_title != row["title"]:
            renamed += 1
        groups.setdefault((row["project"], canonical_title), []).append(row)

    merge_targets = {key: value for key, value in groups.items() if len(value) > 1}
    print(f"rows_considered={len(rows)}")
    print(f"rows_renamed={renamed}")
    print(f"merge_groups={len(merge_targets)}")
    print(f"flagged_generic={flagged}")
    for (project, canonical_title), items in sorted(merge_targets.items()):
        print(f"MERGE {project} :: {canonical_title} :: {len(items)}")
    for row in flagged_rows:
        print(f"FLAG {row['project']} :: {row['title']} :: {row['id']}")

    if not args.apply:
        conn.close()
        return 0

    for (project, canonical_title), items in groups.items():
        survivor = items[0]
        survivor_id = survivor["id"]
        merged_tags: set[str] = set()
        merged_history: list = []
        max_importance = float(survivor["importance_score"] or 0.0)
        best_description = survivor["description"] or ""

        for item in items:
            merged_tags.update(json.loads(item["tags"] or "[]"))
            merged_history.extend(json.loads(item["report_history"] or "[]"))
            max_importance = max(max_importance, float(item["importance_score"] or 0.0))
            if len(item["description"] or "") > len(best_description):
                best_description = item["description"] or ""

        cur.execute(
            """
            UPDATE improvements
            SET title = ?, norm_title = ?, description = ?, importance_score = ?,
                tags = ?, report_count = ?, report_history = ?
            WHERE id = ?
            """,
            (
                canonical_title,
                normalize_title(canonical_title),
                best_description,
                max_importance,
                json.dumps(sorted(merged_tags)),
                len(items),
                json.dumps(merged_history),
                survivor_id,
            ),
        )

        for item in items[1:]:
            cur.execute("DELETE FROM improvements WHERE id = ?", (item["id"],))

    resolved_generic = 0
    for row in flagged_rows:
        tags = set(json.loads(row["tags"] or "[]"))
        tags.update({"generic-skill-gap", "cleanup-reviewed"})
        description = (row["description"] or "").rstrip()
        note = (
            "Marked resolved automatically during skill-gap ontology cleanup because the title "
            "was too generic to remain actionable as an open improvement."
        )
        if note not in description:
            description = f"{description}\n\n{note}".strip()
        cur.execute(
            """
            UPDATE improvements
            SET status = 'resolved',
                resolved_at = ?,
                description = ?,
                tags = ?,
                norm_title = ?
            WHERE id = ?
            """,
            (
                time.time(),
                description,
                json.dumps(sorted(tags)),
                normalize_title(row["title"]),
                row["id"],
            ),
        )
        resolved_generic += 1

    conn.commit()
    conn.close()
    print("applied=true")
    print(f"resolved_generic={resolved_generic}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
