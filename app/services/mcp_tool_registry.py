from __future__ import annotations

import json
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from app.services.llm_gateway import get_cloud_gateway


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _PROJECT_ROOT / "qdrant_data" / "mcp_tool_lifecycle.db"
_DEFAULT_TOOL_STAGE = "stable"
_VALID_STAGES = {"testing", "stable", "deprecated"}

# Current explicit overrides. New tools are auto-seeded as testing once they
# appear in the catalog after a previous bootstrap.
_TOOL_STAGES: dict[str, str] = {
    "normalize_mcp_intent": "testing",
    "list_tool_families": "testing",
    "tool_family_tools": "testing",
    "tool_explain": "testing",
    "tool_recommend": "testing",
    "tool_feedback": "testing",
    "project_work": "testing",
    "project_rules": "testing",
    "project_context": "testing",
    "project_verify": "testing",
    "project_capture": "testing",
    "get_project_reconstruction_bundle": "testing",
    "pull_task_context": "testing",
    "draft_task_checkpoint": "testing",
    "get_work_session_state": "testing",
    "start_work_session": "testing",
    "claim_task": "testing",
    "heartbeat_task_claim": "testing",
    "release_task_claim": "testing",
    "list_task_claims": "testing",
    "park_work_session": "testing",
    "resume_work_session": "testing",
    "end_work_session": "testing",
    "record_stenographer_span": "testing",
    "list_stenographer_spans": "testing",
    "clerk_draft_report": "testing",
    "draft_checkpoint_from_spans": "testing",
    "get_checkpoint_draft": "testing",
    "revise_checkpoint_draft": "testing",
    "approve_checkpoint_draft": "testing",
    "reject_checkpoint_draft": "testing",
    "record_work_result": "testing",
    "record_task_checkpoint": "testing",
    "report_task_checkpoint": "testing",
    "operational_tray": "testing",
    "upsert_knowledge_tree_node": "testing",
    "get_task_execution_context": "testing",
    "reconcile_completed_checkpoints": "testing",
    "review_completed_checkpoint_scope": "testing",
    "review_completed_checkpoint_scopes": "testing",
    "project_rule_candidates_from_stenography": "testing",
    "list_rule_candidates": "testing",
    "get_rule_candidate_review_packet": "testing",
    "review_rule_candidate": "testing",
    "promote_rule_candidate": "testing",
    "revise_law_from_rule_candidate": "testing",
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tool_lifecycle (
    tool_name             TEXT PRIMARY KEY,
    stage                 TEXT NOT NULL DEFAULT 'stable',
    created_at            REAL NOT NULL,
    first_seen_at         REAL NOT NULL,
    last_seen_at          REAL NOT NULL,
    usage_count           INTEGER NOT NULL DEFAULT 0,
    feedback_count        INTEGER NOT NULL DEFAULT 0,
    positive_feedback_count INTEGER NOT NULL DEFAULT 0,
    negative_feedback_count INTEGER NOT NULL DEFAULT 0,
    mixed_feedback_count  INTEGER NOT NULL DEFAULT 0,
    last_feedback_at      REAL NOT NULL DEFAULT 0,
    last_feedback_json    TEXT NOT NULL DEFAULT '{}',
    last_review_at       REAL NOT NULL DEFAULT 0,
    last_review_source   TEXT NOT NULL DEFAULT '',
    last_review_reason   TEXT NOT NULL DEFAULT '',
    last_decision        TEXT NOT NULL DEFAULT '',
    last_decision_source TEXT NOT NULL DEFAULT '',
    last_decision_at     REAL NOT NULL DEFAULT 0,
    catalog_first_seen_at REAL NOT NULL DEFAULT 0,
    catalog_last_seen_at  REAL NOT NULL DEFAULT 0
);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE_SQL)
    return conn


def _normalize_stage(stage: str | None) -> str:
    value = str(stage or "").strip().lower()
    return value if value in _VALID_STAGES else _DEFAULT_TOOL_STAGE


def _stage_for_bootstrap(tool_name: str, *, existing_count: int) -> str:
    explicit = _TOOL_STAGES.get(tool_name)
    if explicit:
        return _normalize_stage(explicit)
    return "stable" if existing_count == 0 else "testing"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _row_exists(conn: sqlite3.Connection, tool_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tool_lifecycle WHERE tool_name = ?",
        (tool_name,),
    ).fetchone()
    return row is not None


def _current_stage(tool_name: str) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT stage FROM tool_lifecycle WHERE tool_name = ?",
            (tool_name,),
        ).fetchone()
    if row is not None:
        return _normalize_stage(row["stage"])
    return _normalize_stage(_TOOL_STAGES.get(tool_name))


def bootstrap_tool_lifecycle(
    tool_names: Iterable[str],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    names = sorted({str(name).strip() for name in tool_names if str(name).strip()})
    now = time.time()
    created = 0
    updated = 0
    skipped = 0
    auto_testing = 0
    with _connect() as conn:
        existing_count = int(
            conn.execute("SELECT COUNT(*) FROM tool_lifecycle").fetchone()[0] or 0
        )
        for tool_name in names:
            row = conn.execute(
                "SELECT tool_name, stage FROM tool_lifecycle WHERE tool_name = ?",
                (tool_name,),
            ).fetchone()
            stage = _stage_for_bootstrap(tool_name, existing_count=existing_count)
            if row is not None:
                if overwrite:
                    conn.execute(
                        """
                        UPDATE tool_lifecycle
                           SET stage = ?,
                               catalog_last_seen_at = ?,
                               last_review_at = ?,
                               last_review_source = ?,
                               last_review_reason = ?,
                               last_decision = ?,
                               last_decision_source = ?,
                               last_decision_at = ?
                         WHERE tool_name = ?
                        """,
                        (
                            stage,
                            now,
                            now,
                            "bootstrap",
                            "catalog resync",
                            "bootstrap",
                            "bootstrap",
                            now,
                            tool_name,
                        ),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """
                        UPDATE tool_lifecycle
                           SET catalog_last_seen_at = ?
                         WHERE tool_name = ?
                        """,
                        (now, tool_name),
                    )
                    skipped += 1
                continue

            if stage == "testing" and _TOOL_STAGES.get(tool_name) is None:
                auto_testing += 1

            conn.execute(
                """
                INSERT INTO tool_lifecycle (
                    tool_name, stage, created_at, first_seen_at, last_seen_at,
                    usage_count, feedback_count, positive_feedback_count,
                    negative_feedback_count, mixed_feedback_count,
                    last_feedback_at, last_feedback_json,
                    last_review_at, last_review_source, last_review_reason,
                    last_decision, last_decision_source, last_decision_at,
                    catalog_first_seen_at, catalog_last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, '{}', 0, '', '', '', '', 0, ?, ?)
                """,
                (tool_name, stage, now, now, now, now, now),
            )
            created += 1
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "auto_testing": auto_testing,
        "total": len(names),
    }


def observe_tool_use(tool_name: str) -> dict[str, Any]:
    name = str(tool_name or "").strip()
    if not name:
        return {}
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tool_lifecycle WHERE tool_name = ?",
            (name,),
        ).fetchone()
        if row is None:
            existing_count = int(
                conn.execute("SELECT COUNT(*) FROM tool_lifecycle").fetchone()[0] or 0
            )
            stage = _stage_for_bootstrap(name, existing_count=existing_count)
            conn.execute(
                """
                INSERT INTO tool_lifecycle (
                    tool_name, stage, created_at, first_seen_at, last_seen_at,
                    usage_count, feedback_count, positive_feedback_count,
                    negative_feedback_count, mixed_feedback_count,
                    last_feedback_at, last_feedback_json,
                    last_review_at, last_review_source, last_review_reason,
                    last_decision, last_decision_source, last_decision_at,
                    catalog_first_seen_at, catalog_last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, 1, 0, 0, 0, 0, 0, '{}', 0, '', '', '', '', 0, ?, ?)
                """,
                (name, stage, now, now, now, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE tool_lifecycle
                   SET usage_count = usage_count + 1,
                       last_seen_at = ?,
                       catalog_last_seen_at = CASE WHEN catalog_last_seen_at = 0 THEN ? ELSE catalog_last_seen_at END
                 WHERE tool_name = ?
                """,
                (now, now, name),
            )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM tool_lifecycle WHERE tool_name = ?",
            (name,),
        ).fetchone()
    return _row_to_dict(updated)


def record_tool_feedback(
    *,
    tool_name: str,
    valence: str,
    tool_stage: str = "testing",
    worked: bool | None = None,
    friction: str = "",
    suggestion: str = "",
    task_context: str = "",
    project_id: str = "",
    agent_id: str = "",
    session_id: str = "",
    missing_fields: Iterable[str] | None = None,
) -> dict[str, Any]:
    name = str(tool_name or "").strip()
    if not name:
        return {}
    stage = _normalize_stage(tool_stage)
    normalized_valence = str(valence or "").strip().lower()
    if normalized_valence not in {"positive", "negative", "mixed"}:
        normalized_valence = "mixed"
    now = time.time()
    payload = {
        "tool_name": name,
        "tool_stage": stage,
        "valence": normalized_valence,
        "worked": worked,
        "friction": str(friction or "").strip(),
        "suggestion": str(suggestion or "").strip(),
        "task_context": str(task_context or "").strip(),
        "project_id": str(project_id or "").strip(),
        "agent_id": str(agent_id or "").strip(),
        "session_id": str(session_id or "").strip(),
        "missing_fields": [str(item).strip() for item in (missing_fields or []) if str(item).strip()],
    }
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tool_lifecycle WHERE tool_name = ?",
            (name,),
        ).fetchone()
        if row is None:
            existing_count = int(
                conn.execute("SELECT COUNT(*) FROM tool_lifecycle").fetchone()[0] or 0
            )
            init_stage = stage if stage in _VALID_STAGES else _stage_for_bootstrap(name, existing_count=existing_count)
            conn.execute(
                """
                INSERT INTO tool_lifecycle (
                    tool_name, stage, created_at, first_seen_at, last_seen_at,
                    usage_count, feedback_count, positive_feedback_count,
                    negative_feedback_count, mixed_feedback_count,
                    last_feedback_at, last_feedback_json,
                    last_review_at, last_review_source, last_review_reason,
                    last_decision, last_decision_source, last_decision_at,
                    catalog_first_seen_at, catalog_last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, 0, '', '', '', '', 0, ?, ?)
                """,
                (
                    name,
                    init_stage,
                    now,
                    now,
                    now,
                    1 if normalized_valence == "positive" else 0,
                    1 if normalized_valence == "negative" else 0,
                    1 if normalized_valence == "mixed" else 0,
                    now,
                    json.dumps(payload),
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE tool_lifecycle
                   SET feedback_count = feedback_count + 1,
                       positive_feedback_count = positive_feedback_count + ?,
                       negative_feedback_count = negative_feedback_count + ?,
                       mixed_feedback_count = mixed_feedback_count + ?,
                       last_feedback_at = ?,
                       last_feedback_json = ?,
                       last_seen_at = ?
                 WHERE tool_name = ?
                """,
                (
                    1 if normalized_valence == "positive" else 0,
                    1 if normalized_valence == "negative" else 0,
                    1 if normalized_valence == "mixed" else 0,
                    now,
                    json.dumps(payload),
                    now,
                    name,
                ),
            )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM tool_lifecycle WHERE tool_name = ?",
            (name,),
        ).fetchone()
    return _row_to_dict(updated)


def get_tool_stage(tool_name: str) -> str:
    return _current_stage(tool_name)


def tool_feedback_expected(tool_name: str) -> bool:
    return get_tool_stage(tool_name) == "testing"


def list_testing_tools() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool_name FROM tool_lifecycle WHERE stage = 'testing' ORDER BY tool_name"
        ).fetchall()
    if rows:
        return [str(row[0]) for row in rows]
    return sorted(name for name, stage in _TOOL_STAGES.items() if stage == "testing")


def tool_stage_payload(tool_name: str) -> dict[str, Any]:
    stage = get_tool_stage(tool_name)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT usage_count, feedback_count, positive_feedback_count,
                   negative_feedback_count, mixed_feedback_count,
                   last_review_at, last_review_source, last_review_reason,
                   last_decision, last_decision_source, last_decision_at
              FROM tool_lifecycle
             WHERE tool_name = ?
            """,
            (str(tool_name or "").strip(),),
        ).fetchone()
    counts = _row_to_dict(row)
    return {
        "tool_name": tool_name,
        "stage": stage,
        "feedback_expected": stage == "testing",
        "usage_count": int(counts.get("usage_count") or 0),
        "feedback_count": int(counts.get("feedback_count") or 0),
        "positive_feedback_count": int(counts.get("positive_feedback_count") or 0),
        "negative_feedback_count": int(counts.get("negative_feedback_count") or 0),
        "mixed_feedback_count": int(counts.get("mixed_feedback_count") or 0),
        "last_review_at": float(counts.get("last_review_at") or 0.0),
        "last_review_source": counts.get("last_review_source") or "",
        "last_review_reason": counts.get("last_review_reason") or "",
        "last_decision": counts.get("last_decision") or "",
        "last_decision_source": counts.get("last_decision_source") or "",
        "last_decision_at": float(counts.get("last_decision_at") or 0.0),
    }


def _parse_feedback_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _summarize_feedback_rows(rows: list[dict[str, Any]], tool_name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = _parse_feedback_json(str(row.get("payload_json") or "{}"))
        if str(payload.get("tool_name") or "").strip() != tool_name:
            continue
        items.append(
            {
                "valence": str(row.get("valence") or "").strip().lower(),
                "source": str(row.get("source") or "").strip(),
                "worked": payload.get("worked"),
                "friction": str(payload.get("friction") or "").strip(),
                "suggestion": str(payload.get("suggestion") or "").strip(),
                "missing_fields": payload.get("missing_fields") or [],
                "task_context": str(payload.get("task_context") or "").strip(),
                "ts": float(row.get("ts") or 0.0),
            }
        )
    return items


async def _llm_review_tool(
    *,
    tool_name: str,
    description: str,
    stage: str,
    usage_count: int,
    feedback_count: int,
    positive_count: int,
    negative_count: int,
    mixed_count: int,
    feedback_examples: list[dict[str, Any]],
    ollama,
) -> tuple[str, str]:
    prompt = {
        "tool_name": tool_name,
        "description": description,
        "current_stage": stage,
        "usage_count": usage_count,
        "feedback_count": feedback_count,
        "positive_feedback_count": positive_count,
        "negative_feedback_count": negative_count,
        "mixed_feedback_count": mixed_count,
        "feedback_examples": feedback_examples[-5:],
        "task": (
            "Decide whether this MCP tool should remain testing, move to stable, or be marked deprecated. "
            "Return JSON only with keys decision and reason. decision must be one of stable, deprecated, testing."
        ),
    }
    raw = await get_cloud_gateway().generate(
        json.dumps(prompt, ensure_ascii=False, indent=2),
        task_type="text_summarization",
        mode="economy",
        max_tokens=200,
        temperature=0.0,
        timeout=45.0,
        allow_local_fallback=True,
        prefer_local=True,
    )
    if not raw:
        return "testing", "LLM unavailable"
    candidate = raw.strip()
    try:
        parsed = json.loads(candidate)
    except Exception:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return "testing", f"Unparseable LLM response: {candidate[:200]}"
        try:
            parsed = json.loads(candidate[start : end + 1])
        except Exception:
            return "testing", f"Unparseable LLM response: {candidate[:200]}"
    decision = _normalize_stage(str(parsed.get("decision") or parsed.get("stage") or "testing"))
    reason = str(parsed.get("reason") or parsed.get("rationale") or "").strip() or "LLM review"
    return decision, reason


async def review_due_tool_lifecycles(
    *,
    tool_catalog: Iterable[dict[str, Any]] | None = None,
    ollama=None,
    min_age_days: float = 7.0,
    max_age_days: float = 21.0,
    min_feedback: int = 3,
) -> dict[str, Any]:
    catalog = list(tool_catalog or [])
    catalog_by_name = {
        str(item.get("name") or "").strip(): item
        for item in catalog
        if str(item.get("name") or "").strip()
    }
    bootstrap_tool_lifecycle(catalog_by_name.keys())
    now = time.time()
    reviewed = 0
    promoted = 0
    deprecated = 0
    kept_testing = 0
    llm_used = 0
    decisions: list[dict[str, Any]] = []

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tool_lifecycle
             WHERE stage = 'testing'
             ORDER BY last_seen_at DESC, tool_name ASC
            """
        ).fetchall()

    for row in rows:
        item = _row_to_dict(row)
        tool_name = str(item.get("tool_name") or "").strip()
        if not tool_name:
            continue
        created_at = float(item.get("catalog_first_seen_at") or item.get("first_seen_at") or item.get("created_at") or now)
        age_days = max(0.0, (now - created_at) / 86400.0)
        usage_count = int(item.get("usage_count") or 0)
        feedback_count = int(item.get("feedback_count") or 0)
        positive_count = int(item.get("positive_feedback_count") or 0)
        negative_count = int(item.get("negative_feedback_count") or 0)
        mixed_count = int(item.get("mixed_feedback_count") or 0)
        if feedback_count == 0 and age_days < min_age_days:
            kept_testing += 1
            continue

        description = str(catalog_by_name.get(tool_name, {}).get("description") or "").strip()
        feedback_examples = []
        last_feedback_raw = str(item.get("last_feedback_json") or "{}")
        last_feedback = _parse_feedback_json(last_feedback_raw)
        if last_feedback:
            feedback_examples.append(last_feedback)
        decision = "testing"
        reason = "Awaiting enough feedback to justify a lifecycle decision."

        strong_positive = feedback_count >= min_feedback and negative_count == 0 and positive_count >= max(2, min_feedback - 1)
        strong_negative = feedback_count >= min_feedback and negative_count >= max(2, positive_count) and negative_count >= 2
        enough_time = age_days >= max_age_days

        if strong_positive:
            decision = "stable"
            reason = "Feedback is consistently positive and no negative signal was observed."
        elif strong_negative:
            decision = "deprecated"
            reason = "Feedback is consistently negative or clearly outweighed by negative signal."
        elif (feedback_count >= min_feedback and age_days >= min_age_days) or enough_time:
            if ollama is not None:
                try:
                    decision, reason = await _llm_review_tool(
                        tool_name=tool_name,
                        description=description,
                        stage="testing",
                        usage_count=usage_count,
                        feedback_count=feedback_count,
                        positive_count=positive_count,
                        negative_count=negative_count,
                        mixed_count=mixed_count,
                        feedback_examples=feedback_examples,
                        ollama=ollama,
                    )
                    llm_used += 1
                except Exception as exc:
                    decision = "testing"
                    reason = f"LLM review failed: {exc}"
            else:
                reason = "Need more feedback before promoting or deprecating."

        if decision not in {"stable", "deprecated"}:
            kept_testing += 1
            with _connect() as conn:
                conn.execute(
                    """
                    UPDATE tool_lifecycle
                       SET last_review_at = ?,
                           last_review_source = ?,
                           last_review_reason = ?,
                           last_decision = ?,
                           last_decision_source = ?,
                           last_decision_at = ?
                     WHERE tool_name = ?
                    """,
                    (now, "background_review", reason, "testing", "background_review", now, tool_name),
                )
                conn.commit()
            continue

        reviewed += 1
        if decision == "stable":
            promoted += 1
        elif decision == "deprecated":
            deprecated += 1

        with _connect() as conn:
            conn.execute(
                """
                UPDATE tool_lifecycle
                   SET stage = ?,
                       last_review_at = ?,
                       last_review_source = ?,
                       last_review_reason = ?,
                       last_decision = ?,
                       last_decision_source = ?,
                       last_decision_at = ?
                 WHERE tool_name = ?
                """,
                (decision, now, "background_review", reason, decision, "background_review", now, tool_name),
            )
            conn.commit()
        decisions.append(
            {
                "tool_name": tool_name,
                "decision": decision,
                "reason": reason,
                "age_days": round(age_days, 2),
                "feedback_count": feedback_count,
                "positive_feedback_count": positive_count,
                "negative_feedback_count": negative_count,
            }
        )

    return {
        "reviewed": reviewed,
        "promoted": promoted,
        "deprecated": deprecated,
        "kept_testing": kept_testing,
        "llm_used": llm_used,
        "decisions": decisions,
    }
