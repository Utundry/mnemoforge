from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


TARGET_EVENT_TYPES = ("dialogue_excerpt", "dialogue_signal", "artifact_suggested")


def find_duplicate_ids(conn: sqlite3.Connection) -> list[int]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id
        FROM events
        WHERE event_type IN (?, ?, ?)
          AND id NOT IN (
            SELECT MIN(id)
            FROM events
            WHERE event_type IN (?, ?, ?)
            GROUP BY event_type, episode_id, agent_id, context_signature, payload_json
          )
        ORDER BY id
        """,
        TARGET_EVENT_TYPES + TARGET_EVENT_TYPES,
    ).fetchall()
    return [int(row[0]) for row in rows]


def summarize(conn: sqlite3.Connection) -> list[tuple[str, str, str, int]]:
    cur = conn.cursor()
    return cur.execute(
        """
        SELECT event_type, episode_id, agent_id, COUNT(*) AS n
        FROM events
        WHERE event_type IN (?, ?, ?)
        GROUP BY event_type, episode_id, agent_id, context_signature, payload_json
        HAVING COUNT(*) > 1
        ORDER BY n DESC, event_type, episode_id, agent_id
        """,
        TARGET_EVENT_TYPES,
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove exact duplicate watcher learning events.")
    parser.add_argument(
        "--db",
        default=str(Path("qdrant_data") / "learning.db"),
        help="Path to learning.db",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicate rows. Default is dry-run.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        dup_groups = summarize(conn)
        dup_ids = find_duplicate_ids(conn)

        print(f"duplicate_groups={len(dup_groups)}")
        print(f"duplicate_rows={len(dup_ids)}")
        for row in dup_groups[:20]:
            print(row)

        if not args.apply:
            return 0

        if dup_ids:
            conn.executemany("DELETE FROM events WHERE id = ?", [(row_id,) for row_id in dup_ids])
            conn.commit()
        print(f"deleted_rows={len(dup_ids)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
