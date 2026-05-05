from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.text_localization import looks_like_mojibake

QDRANT_DATA = ROOT / "qdrant_data"

LEARNING_DB = QDRANT_DATA / "learning.db"
IMPROVEMENTS_DB = QDRANT_DATA / "improvements.db"
PERFORMANCE_DB = QDRANT_DATA / "performance.db"
CAPABILITIES_JSON = QDRANT_DATA / "capabilities.json"
MODEL_REGISTRY_JSON = QDRANT_DATA / "model_registry.json"

TELEMETRY_MARKERS = (
    "candidate_approved",
    "candidate_rejected",
    "memory_write",
    "tool_call",
)

PRACTICAL_CHECK_PREFIX = "practical-check-"
TEST_IMPROVEMENT_PROJECT = "proj-x"
TEST_IMPROVEMENT_TITLE = "Test improvement"
TEST_IMPROVEMENT_AGENT_ID = "tester"
TEST_IMPROVEMENT_DESCRIPTION = "Desc"


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


def _artifact_cleanup_reasons(row: sqlite3.Row) -> list[str]:
    reasons: list[str] = []
    content = str(row["content"] or "")
    observation = str(row["observation"] or "")
    meta = _load_json(row["meta_json"])
    signal_type = str(meta.get("signal_type") or "")
    blob = "\n".join([content, observation, json.dumps(meta, ensure_ascii=False)])
    blob_lower = blob.lower()

    if any(marker in blob_lower for marker in TELEMETRY_MARKERS):
        reasons.append("telemetry_derived_rule")

    if signal_type == "new_terminology":
        if "mnemoforge" in blob_lower and "new term" in blob_lower:
            reasons.append("project_term_misclassified_as_new_terminology")

    if signal_type in {"new_terminology", "skill_gap"}:
        if "x-api-key" in blob_lower:
            reasons.append("dialogue_excerpt_contaminated_by_operational_error")
        if looks_like_mojibake(blob):
            reasons.append("dialogue_excerpt_contains_mojibake")

    return reasons


def cleanup_learning(*, apply: bool) -> dict:
    conn = sqlite3.connect(str(LEARNING_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, artifact_scope, artifact_type, action_type, status, content, observation, meta_json
        FROM artifacts
        WHERE status = 'active'
        ORDER BY updated_at DESC
        """
    ).fetchall()

    matches: list[tuple[str, list[str]]] = []
    now = time.time()
    for row in rows:
        reasons = _artifact_cleanup_reasons(row)
        if not reasons:
            continue
        matches.append((str(row["id"]), reasons))
        if apply:
            meta = _load_json(row["meta_json"])
            meta["cleanup"] = {
                "reasons": reasons,
                "cleaned_at": now,
                "cleaned_by": "cleanup_live_anomalies",
            }
            cur.execute(
                "UPDATE artifacts SET status = 'archived', updated_at = ?, meta_json = ? WHERE id = ?",
                (now, _dump_json(meta), str(row["id"])),
            )

    if apply:
        conn.commit()
    conn.close()
    return {"matched": len(matches), "artifact_ids": matches}


def cleanup_improvements(*, apply: bool) -> dict:
    conn = sqlite3.connect(str(IMPROVEMENTS_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, project, title, agent_id, status, description
        FROM improvements
        WHERE project = ?
          AND title = ?
          AND agent_id = ?
          AND description = ?
        ORDER BY created_at DESC
        """,
        (
            TEST_IMPROVEMENT_PROJECT,
            TEST_IMPROVEMENT_TITLE,
            TEST_IMPROVEMENT_AGENT_ID,
            TEST_IMPROVEMENT_DESCRIPTION,
        ),
    ).fetchall()
    ids = [str(row["id"]) for row in rows]
    if apply and ids:
        cur.executemany("DELETE FROM improvements WHERE id = ?", [(rid,) for rid in ids])
        conn.commit()
    conn.close()
    return {"matched": len(ids), "ids": ids}


def cleanup_performance(*, apply: bool) -> dict:
    conn = sqlite3.connect(str(PERFORMANCE_DB))
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, component
        FROM task_events
        WHERE component LIKE ?
        ORDER BY id
        """,
        (f"{PRACTICAL_CHECK_PREFIX}%",),
    ).fetchall()
    ids = [int(row[0]) for row in rows]
    if apply and ids:
        cur.executemany("DELETE FROM task_events WHERE id = ?", [(rid,) for rid in ids])
        conn.commit()
    conn.close()
    return {"matched": len(ids), "ids": ids}


def _cleanup_json_map(path: Path, *, apply: bool) -> dict:
    if not path.exists():
        return {"matched": 0, "keys": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    doomed = [
        key
        for key, value in data.items()
        if key.startswith(PRACTICAL_CHECK_PREFIX) or str((value or {}).get("provider") or "") == "test"
    ]
    if apply and doomed:
        for key in doomed:
            data.pop(key, None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"matched": len(doomed), "keys": doomed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean deterministic live-data anomalies from qdrant_data.")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup mutations. Default is dry-run.")
    args = parser.parse_args()

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "learning_artifacts": cleanup_learning(apply=args.apply),
        "improvements": cleanup_improvements(apply=args.apply),
        "performance": cleanup_performance(apply=args.apply),
        "capabilities": _cleanup_json_map(CAPABILITIES_JSON, apply=args.apply),
        "model_registry": _cleanup_json_map(MODEL_REGISTRY_JSON, apply=args.apply),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
