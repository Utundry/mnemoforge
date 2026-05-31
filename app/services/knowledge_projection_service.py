from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from app.models.law import ProjectLawRecord
from app.services.system_data_root import data_path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = data_path("knowledge_projections.db")
_PROJECTION_VERSION = "1"

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_projections (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    source_version TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    content TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (entity_type, entity_id, variant)
);
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    return conn


def _upsert_projection(
    *,
    entity_type: str,
    entity_id: str,
    variant: str,
    source_version: str,
    content: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_projections(
                entity_type, entity_id, variant, source_version, projection_version,
                content, generated_at, stale, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 0, ?)
            ON CONFLICT(entity_type, entity_id, variant) DO UPDATE SET
                source_version=excluded.source_version,
                projection_version=excluded.projection_version,
                content=excluded.content,
                generated_at=datetime('now'),
                stale=0,
                meta_json=excluded.meta_json
            """,
            (
                entity_type,
                entity_id,
                variant,
                source_version,
                _PROJECTION_VERSION,
                content,
                json.dumps(meta, ensure_ascii=True),
            ),
        )
        row = conn.execute(
            """
            SELECT entity_type, entity_id, variant, source_version, projection_version,
                   content, generated_at, stale, meta_json
            FROM knowledge_projections
            WHERE entity_type=? AND entity_id=? AND variant=?
            """,
            (entity_type, entity_id, variant),
        ).fetchone()
    return {
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "variant": row["variant"],
        "source_version": row["source_version"],
        "projection_version": row["projection_version"],
        "content": row["content"],
        "generated_at": row["generated_at"],
        "stale": bool(row["stale"]),
        "meta": json.loads(row["meta_json"] or "{}"),
    }


def _get_projection(entity_type: str, entity_id: str, variant: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT entity_type, entity_id, variant, source_version, projection_version,
                   content, generated_at, stale, meta_json
            FROM knowledge_projections
            WHERE entity_type=? AND entity_id=? AND variant=?
            """,
            (entity_type, entity_id, variant),
        ).fetchone()
    if row is None:
        return None
    return {
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "variant": row["variant"],
        "source_version": row["source_version"],
        "projection_version": row["projection_version"],
        "content": row["content"],
        "generated_at": row["generated_at"],
        "stale": bool(row["stale"]),
        "meta": json.loads(row["meta_json"] or "{}"),
    }


def _get_or_build_projection(
    *,
    entity_type: str,
    entity_id: str,
    variant: str,
    source_version: str,
    builder: Callable[[], tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    current = _get_projection(entity_type, entity_id, variant)
    if (
        current is not None
        and current["source_version"] == source_version
        and current["projection_version"] == _PROJECTION_VERSION
    ):
        return current
    content, meta = builder()
    return _upsert_projection(
        entity_type=entity_type,
        entity_id=entity_id,
        variant=variant,
        source_version=source_version,
        content=content,
        meta=meta,
    )


def _law_source_version(law: ProjectLawRecord) -> str:
    return "|".join(
        [
            law.id,
            law.updated_at.isoformat(),
            law.status,
            law.version,
            str(bool(law.candidate_revision)),
        ]
    )


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _render_law_projection(law: ProjectLawRecord, *, variant: str) -> tuple[str, dict[str, Any]]:
    locality = "project-local" if law.is_project_local else law.scope
    if variant == "compact":
        content = f"- [{locality}] {law.title}: {_truncate(law.statement, 220)}"
        if law.rationale:
            content += f"\n  Why: {_truncate(law.rationale, 180)}"
    else:
        lines = [f"- [{locality}] {law.title}: {law.statement}"]
        if law.rationale:
            lines.append(f"  Why: {law.rationale}")
        if law.evidence:
            lines.append("  Evidence: " + "; ".join(law.evidence[:5]))
        content = "\n".join(lines)
    return content, {
        "title": law.title,
        "project": law.project,
        "scope": law.scope,
        "status": law.status,
        "variant": variant,
    }


def get_or_build_law_projection(law: ProjectLawRecord, *, variant: str = "compact") -> dict[str, Any]:
    return _get_or_build_projection(
        entity_type="law",
        entity_id=law.id,
        variant=variant,
        source_version=_law_source_version(law),
        builder=lambda: _render_law_projection(law, variant=variant),
    )


def build_law_projection_block(laws: list[ProjectLawRecord], *, variant: str = "compact") -> str:
    if not laws:
        return ""
    lines = ["## Applicable Project Laws", ""]
    for law in laws:
        projection = get_or_build_law_projection(law, variant=variant)
        lines.append(projection["content"])
    return "\n".join(lines)
