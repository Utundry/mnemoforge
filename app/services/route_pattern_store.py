from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.learning_eligibility_service import evaluate_learning_eligibility
from app.services.system_data_root import data_path
from app.services.mcp_workflow_specs import load_route_catalog_spec

_DB_PATH = data_path("route_patterns.db")
_VERY_LOW_CONFIDENCE_THRESHOLD = 0.2
_NO_HIT_LOW_EVIDENCE_CONFIDENCE_THRESHOLD = 0.55
_NO_HIT_LOW_EVIDENCE_PROBATION_DAYS = 3
_DIAGNOSTIC_META_MARKERS = (
    "diagnostic",
    "route_hygiene",
    "misroute",
    "misclassification",
    "failed_route",
    "problem_report",
    "hygiene_review",
)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS route_patterns (
    id TEXT PRIMARY KEY,
    facade TEXT NOT NULL,
    normalized_pattern TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    tokens_json TEXT NOT NULL DEFAULT '[]',
    simhash64 TEXT NOT NULL DEFAULT '0',
    intent_type TEXT NOT NULL,
    tool TEXT NOT NULL,
    mutating INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.0,
    source TEXT NOT NULL DEFAULT 'llm',
    evidence_count INTEGER NOT NULL DEFAULT 1,
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    positive_feedback INTEGER NOT NULL DEFAULT 0,
    negative_feedback INTEGER NOT NULL DEFAULT 0,
    last_feedback_at REAL NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_route_patterns_exact
ON route_patterns(facade, pattern_hash)
WHERE disabled = 0;

CREATE INDEX IF NOT EXISTS idx_route_patterns_facade
ON route_patterns(facade, disabled, updated_at DESC);
"""

_MIGRATION_COLUMNS = {
    "positive_feedback": "INTEGER NOT NULL DEFAULT 0",
    "negative_feedback": "INTEGER NOT NULL DEFAULT 0",
    "last_feedback_at": "REAL NOT NULL DEFAULT 0",
}


def _normalize_pattern(text: str) -> str:
    clean = str(text or "").casefold()
    clean = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", " <uuid> ", clean)
    clean = re.sub(r"\b[0-9a-f]{8}(?:-[0-9a-f]{1,4}){0,4}\b", " <id-prefix> ", clean)
    clean = re.sub(r"[^\w<>]+", " ", clean, flags=re.UNICODE)
    return re.sub(r"\s{2,}", " ", clean).strip()


def _pattern_tokens(text: str) -> list[str]:
    return [token for token in _normalize_pattern(text).split(" ") if len(token) >= 3]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / (len(a | b) or 1)


def _simhash64(tokens: list[str]) -> int:
    if not tokens:
        return 0
    weights: dict[str, int] = {}
    for token in tokens:
        weights[token] = weights.get(token, 0) + 1
    vector = [0] * 64
    for token, weight in weights.items():
        digest = int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(64):
            vector[i] += weight if ((digest >> i) & 1) else -weight
    out = 0
    for i, value in enumerate(vector):
        if value > 0:
            out |= 1 << i
    return out


def _hamming64(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


def _row_to_route(row: sqlite3.Row, *, backend_used: str, score: float, matched_by: str) -> dict[str, Any]:
    metadata = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except Exception:
        metadata = {}
    return {
        "pattern_id": row["id"],
        "facade": row["facade"],
        "intent_type": row["intent_type"],
        "tool": row["tool"],
        "mutating": bool(row["mutating"]),
        "confidence": round(min(1.0, max(float(row["confidence"] or 0.0), score)), 3),
        "matched_example": str(metadata.get("matched_example") or "learned_route_pattern"),
        "reason": str(metadata.get("reason") or "Matched a learned route pattern."),
        "backend_used": backend_used,
        "score": round(score, 3),
        "matched_by": matched_by,
        "metadata": metadata,
    }


def _row_metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
        return metadata if isinstance(metadata, dict) else {}
    except Exception:
        return {}


def _row_to_pattern(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _row_metadata(row)
    return {
        "pattern_id": row["id"],
        "facade": row["facade"],
        "normalized_pattern": row["normalized_pattern"],
        "intent_type": row["intent_type"],
        "tool": row["tool"],
        "mutating": bool(row["mutating"]),
        "confidence": round(float(row["confidence"] or 0.0), 3),
        "source": row["source"],
        "evidence_count": int(row["evidence_count"] or 0),
        "hit_count": int(row["hit_count"] or 0),
        "positive_feedback": int(row["positive_feedback"] or 0),
        "negative_feedback": int(row["negative_feedback"] or 0),
        "last_hit_at": float(row["last_hit_at"] or 0.0),
        "last_feedback_at": float(row["last_feedback_at"] or 0.0),
        "disabled": bool(row["disabled"]),
        "disabled_reason": str(metadata.get("disabled_reason") or ""),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "metadata": metadata,
    }


def _finding(
    *,
    type: str,
    severity: str,
    item: dict[str, Any],
    reason: str,
    recommended_action: str,
    disposition: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": type,
        "severity": severity,
        "pattern_id": item["pattern_id"],
        "facade": item["facade"],
        "tool": item.get("tool"),
        "intent_type": item.get("intent_type"),
        "reason": reason,
        "recommended_action": recommended_action,
        "disposition": disposition,
        **{key: value for key, value in extra.items() if value not in (None, "", [])},
    }


def _finding_type_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        type_name = str(finding.get("type") or "").strip()
        if not type_name:
            continue
        counts[type_name] = counts.get(type_name, 0) + 1
    return counts


def _catalog_tool_for_intent(facade: str, intent_type: str) -> str:
    try:
        catalog = load_route_catalog_spec(str(facade or "").strip())
    except Exception:
        return ""
    intent = str(intent_type or "").strip()
    for route in catalog.routes:
        if route.intent_type == intent:
            return str(route.tool or "").strip()
    return ""


def _metadata_contains_marker(metadata: dict[str, Any], pattern: str) -> str:
    haystack = " ".join([str(pattern or ""), json.dumps(metadata, ensure_ascii=False, sort_keys=True)]).casefold()
    for marker in _DIAGNOSTIC_META_MARKERS:
        if marker and marker in haystack:
            return marker
    return ""


def _learning_provenance(metadata: dict[str, Any], source: str) -> dict[str, Any]:
    eligibility = metadata.get("learning_eligibility")
    if isinstance(eligibility, dict):
        return {
            "decision": str(eligibility.get("decision") or "").strip(),
            "eligible": bool(eligibility.get("eligible", False)),
            "source": str(source or "").strip(),
        }
    return {"decision": "", "eligible": False, "source": str(source or "").strip()}


class RoutePatternStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._ensure_schema_columns_locked()
            self._conn.commit()

    def _ensure_schema_columns_locked(self) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(route_patterns)").fetchall()
        }
        for column, definition in _MIGRATION_COLUMNS.items():
            if column in existing:
                continue
            self._conn.execute(f"ALTER TABLE route_patterns ADD COLUMN {column} {definition}")

    def record(
        self,
        *,
        facade: str,
        pattern: str,
        intent_type: str,
        tool: str,
        mutating: bool = False,
        confidence: float = 0.0,
        source: str = "llm",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        eligibility = evaluate_learning_eligibility(
            source=source,
            metadata=metadata or {},
            pattern=pattern,
        )
        if not eligibility.get("eligible"):
            return ""
        metadata = eligibility.get("metadata") if isinstance(eligibility.get("metadata"), dict) else metadata
        normalized = _normalize_pattern(pattern)
        if not facade or not normalized or not intent_type or not tool:
            return ""
        now = time.time()
        tokens = _pattern_tokens(normalized)
        pattern_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        pattern_id = hashlib.sha256(f"{facade}:{pattern_hash}".encode("utf-8")).hexdigest()[:32]
        with self._lock:
            existing = self._conn.execute(
                "SELECT evidence_count, created_at FROM route_patterns WHERE facade = ? AND pattern_hash = ?",
                (facade, pattern_hash),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            evidence_count = int(existing["evidence_count"] or 0) + 1 if existing else 1
            self._conn.execute(
                """
                INSERT INTO route_patterns (
                    id, facade, normalized_pattern, pattern_hash, tokens_json, simhash64,
                    intent_type, tool, mutating, confidence, source, evidence_count,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    normalized_pattern = excluded.normalized_pattern,
                    tokens_json = excluded.tokens_json,
                    simhash64 = excluded.simhash64,
                    intent_type = excluded.intent_type,
                    tool = excluded.tool,
                    mutating = excluded.mutating,
                    confidence = max(route_patterns.confidence, excluded.confidence),
                    source = excluded.source,
                    evidence_count = ?,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    pattern_id,
                    facade,
                    normalized,
                    pattern_hash,
                    json.dumps(tokens, ensure_ascii=False),
                    str(_simhash64(tokens)),
                    intent_type,
                    tool,
                    1 if mutating else 0,
                    float(confidence or 0.0),
                    source or "llm",
                    evidence_count,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    created_at,
                    now,
                    evidence_count,
                ),
            )
            self._conn.commit()
        return pattern_id

    def match(
        self,
        *,
        facade: str,
        pattern: str,
        allowed_intent_types: set[str] | None = None,
        semantic_threshold: float = 0.6,
    ) -> dict[str, Any] | None:
        normalized = _normalize_pattern(pattern)
        if not facade or not normalized:
            return None
        allowed = set(allowed_intent_types or set())
        pattern_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        tokens = set(_pattern_tokens(normalized))
        simhash = _simhash64(list(tokens))
        with self._lock:
            exact = self._conn.execute(
                """
                SELECT * FROM route_patterns
                WHERE facade = ? AND pattern_hash = ? AND disabled = 0
                """,
                (facade, pattern_hash),
            ).fetchone()
            if exact and (not allowed or exact["intent_type"] in allowed):
                exact_route = _row_to_route(exact, backend_used="learned_exact", score=1.0, matched_by="exact")
                exact_id = str(exact["id"])
            else:
                exact_route = None
                exact_id = ""

            rows = self._conn.execute(
                """
                SELECT * FROM route_patterns
                WHERE facade = ? AND disabled = 0
                ORDER BY updated_at DESC
                LIMIT 200
                """,
                (facade,),
            ).fetchall()

        if exact_route:
            self._record_hit(exact_id)
            return exact_route

        best: tuple[float, sqlite3.Row] | None = None
        for row in rows:
            if allowed and row["intent_type"] not in allowed:
                continue
            try:
                row_tokens = set(json.loads(row["tokens_json"] or "[]"))
                row_simhash = int(row["simhash64"] or "0")
            except Exception:
                continue
            score = _jaccard(tokens, row_tokens)
            if tokens and row_tokens and _hamming64(simhash, row_simhash) <= 8:
                score = max(score, 0.78)
            if score >= semantic_threshold and (best is None or score > best[0]):
                best = (score, row)

        if not best:
            return None
        self._record_hit(str(best[1]["id"]))
        return _row_to_route(best[1], backend_used="learned_semantic", score=best[0], matched_by="semantic")

    def preview_match(
        self,
        *,
        facade: str,
        pattern: str,
        allowed_intent_types: set[str] | None = None,
        semantic_threshold: float = 0.6,
    ) -> dict[str, Any] | None:
        normalized = _normalize_pattern(pattern)
        if not facade or not normalized:
            return None
        allowed = set(allowed_intent_types or set())
        pattern_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        tokens = set(_pattern_tokens(normalized))
        simhash = _simhash64(list(tokens))
        with self._lock:
            exact = self._conn.execute(
                """
                SELECT * FROM route_patterns
                WHERE facade = ? AND pattern_hash = ? AND disabled = 0
                """,
                (facade, pattern_hash),
            ).fetchone()
            if exact and (not allowed or exact["intent_type"] in allowed):
                return _row_to_route(exact, backend_used="learned_exact", score=1.0, matched_by="exact")

            rows = self._conn.execute(
                """
                SELECT * FROM route_patterns
                WHERE facade = ? AND disabled = 0
                ORDER BY updated_at DESC
                LIMIT 200
                """,
                (facade,),
            ).fetchall()

        best: tuple[float, sqlite3.Row] | None = None
        for row in rows:
            if allowed and row["intent_type"] not in allowed:
                continue
            try:
                row_tokens = set(json.loads(row["tokens_json"] or "[]"))
                row_simhash = int(row["simhash64"] or "0")
            except Exception:
                continue
            score = _jaccard(tokens, row_tokens)
            if tokens and row_tokens and _hamming64(simhash, row_simhash) <= 8:
                score = max(score, 0.78)
            if score >= semantic_threshold and (best is None or score > best[0]):
                best = (score, row)

        if not best:
            return None
        return _row_to_route(best[1], backend_used="learned_semantic", score=best[0], matched_by="semantic")

    def _record_hit(self, pattern_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE route_patterns
                SET hit_count = hit_count + 1, last_hit_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (time.time(), time.time(), pattern_id),
            )
            self._conn.commit()

    def disable_pattern(
        self,
        pattern_id: str,
        *,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        pattern_id = str(pattern_id or "").strip()
        if not pattern_id:
            return False
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata_json FROM route_patterns WHERE id = ? AND disabled = 0",
                (pattern_id,),
            ).fetchone()
            if row is None:
                return False
            current_metadata: dict[str, Any] = {}
            try:
                current_metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                current_metadata = {}
            current_metadata["disabled_reason"] = str(reason or "invalidated").strip() or "invalidated"
            current_metadata["disabled_at"] = now
            if metadata:
                current_metadata["disabled_context"] = metadata
            self._conn.execute(
                """
                UPDATE route_patterns
                SET disabled = 1,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(current_metadata, ensure_ascii=False), now, pattern_id),
            )
            self._conn.commit()
            return True

    def record_feedback(
        self,
        pattern_id: str,
        *,
        vote: str,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        pattern_id = str(pattern_id or "").strip()
        vote_name = str(vote or "").strip().lower()
        if vote_name not in {"positive", "negative"} or not pattern_id:
            return None
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM route_patterns WHERE id = ?",
                (pattern_id,),
            ).fetchone()
            if row is None:
                return None
            current_metadata = _row_metadata(row)
            feedback_events = current_metadata.get("feedback_events")
            if not isinstance(feedback_events, list):
                feedback_events = []
            feedback_events.append(
                {
                    "vote": vote_name,
                    "reason": str(reason or "").strip(),
                    "at": now,
                    "context": metadata or {},
                }
            )
            current_metadata["feedback_events"] = feedback_events[-20:]
            if vote_name == "positive":
                confidence = min(1.0, float(row["confidence"] or 0.0) + 0.04)
                self._conn.execute(
                    """
                    UPDATE route_patterns
                    SET positive_feedback = positive_feedback + 1,
                        evidence_count = evidence_count + 1,
                        confidence = ?,
                        last_feedback_at = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (confidence, now, json.dumps(current_metadata, ensure_ascii=False), now, pattern_id),
                )
            else:
                confidence = max(0.0, float(row["confidence"] or 0.0) - 0.12)
                self._conn.execute(
                    """
                    UPDATE route_patterns
                    SET negative_feedback = negative_feedback + 1,
                        confidence = ?,
                        last_feedback_at = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (confidence, now, json.dumps(current_metadata, ensure_ascii=False), now, pattern_id),
                )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM route_patterns WHERE id = ?",
                (pattern_id,),
            ).fetchone()
        return _row_to_pattern(updated) if updated is not None else None

    def list_patterns(
        self,
        *,
        facade: str = "",
        disabled: bool | None = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        clauses: list[str] = []
        params: list[Any] = []
        facade_name = str(facade or "").strip()
        if facade_name:
            clauses.append("facade = ?")
            params.append(facade_name)
        if disabled is not None:
            clauses.append("disabled = ?")
            params.append(1 if disabled else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM route_patterns
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [_row_to_pattern(row) for row in rows]

    def hygiene_report(
        self,
        *,
        facade: str = "",
        known_tools: set[str] | None = None,
        limit: int = 100,
        stale_after_days: int = 30,
    ) -> dict[str, Any]:
        active = self.list_patterns(facade=facade, disabled=False, limit=limit)
        disabled = self.list_patterns(facade=facade, disabled=True, limit=limit)
        known = {str(tool or "").strip() for tool in (known_tools or set()) if str(tool or "").strip()}
        now = time.time()
        stale_after_seconds = max(1, int(stale_after_days or 30)) * 86400
        low_evidence_probation_seconds = _NO_HIT_LOW_EVIDENCE_PROBATION_DAYS * 86400
        findings: list[dict[str, Any]] = []
        for item in active:
            tool = str(item.get("tool") or "").strip()
            intent_type = str(item.get("intent_type") or "").strip()
            confidence = float(item.get("confidence") or 0.0)
            evidence_count = int(item.get("evidence_count") or 0)
            hit_count = int(item.get("hit_count") or 0)
            updated_at = float(item.get("updated_at") or 0.0)
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if known and tool not in known:
                findings.append(
                    _finding(
                        type="unknown_tool",
                        severity="high",
                        item=item,
                        reason="Learned route points to a tool that is not in the current known tool set.",
                        recommended_action="disable",
                        disposition="disable",
                    )
                )
            negative = int(item.get("negative_feedback") or 0)
            positive = int(item.get("positive_feedback") or 0)
            if negative >= positive + 2:
                findings.append(
                    _finding(
                        type="negative_feedback",
                        severity="medium",
                        item=item,
                        reason="Learned route has more negative than positive feedback.",
                        recommended_action="disable",
                        disposition="disable",
                        positive_feedback=positive,
                        negative_feedback=negative,
                    )
                )
            last_hit = float(item.get("last_hit_at") or 0.0)
            age_source = last_hit or float(item.get("updated_at") or 0.0)
            if age_source and now - age_source > stale_after_seconds and int(item.get("evidence_count") or 0) <= 1:
                findings.append(
                    _finding(
                        type="low_evidence_stale_pattern",
                        severity="low",
                        item=item,
                        reason="Learned route has low evidence and has not been used recently.",
                        recommended_action="request-feedback",
                        disposition="observe",
                    )
                )
            if confidence < _VERY_LOW_CONFIDENCE_THRESHOLD:
                findings.append(
                    _finding(
                        type="very_low_confidence_pattern",
                        severity="high",
                        item=item,
                        reason="Learned route confidence is below the safe active-pattern threshold.",
                        recommended_action="quarantine",
                        disposition="quarantine",
                        confidence=round(confidence, 3),
                        threshold=_VERY_LOW_CONFIDENCE_THRESHOLD,
                    )
                )
            if (
                hit_count == 0
                and evidence_count <= 1
                and confidence < _NO_HIT_LOW_EVIDENCE_CONFIDENCE_THRESHOLD
                and updated_at
                and now - updated_at > low_evidence_probation_seconds
            ):
                findings.append(
                    _finding(
                        type="no_hit_low_evidence_pattern",
                        severity="medium",
                        item=item,
                        reason="Learned route has no hits, only weak evidence, and has passed the short probation window.",
                        recommended_action="request-feedback",
                        disposition="observe",
                        confidence=round(confidence, 3),
                        hit_count=hit_count,
                        evidence_count=evidence_count,
                        probation_days=_NO_HIT_LOW_EVIDENCE_PROBATION_DAYS,
                    )
                )
            marker = _metadata_contains_marker(metadata, str(item.get("normalized_pattern") or ""))
            if marker:
                findings.append(
                    _finding(
                        type="diagnostic_or_meta_contamination",
                        severity="high",
                        item=item,
                        reason="Learned route contains diagnostic, hygiene, misroute, or other meta-analysis markers.",
                        recommended_action="quarantine",
                        disposition="quarantine",
                        marker=marker,
                    )
                )
            expected_tool = _catalog_tool_for_intent(str(item.get("facade") or ""), intent_type)
            if expected_tool and tool and expected_tool != tool:
                findings.append(
                    _finding(
                        type="route_tool_mismatch",
                        severity="high",
                        item=item,
                        reason="Learned route tool does not match the current route catalog for its facade and intent_type.",
                        recommended_action="quarantine",
                        disposition="quarantine",
                        expected_tool=expected_tool,
                        actual_tool=tool,
                    )
                )
            provenance = _learning_provenance(metadata, str(item.get("source") or ""))
            if (
                not provenance["decision"]
                or (
                    str(provenance["source"]).casefold() == "llm"
                    and provenance["decision"] == "default"
                    and positive <= 0
                    and hit_count <= 0
                )
            ):
                findings.append(
                    _finding(
                        type="weak_learning_provenance",
                        severity="medium",
                        item=item,
                        reason="Learned route lacks strong operator/user feedback provenance.",
                        recommended_action="request-feedback",
                        disposition="observe",
                        source=provenance["source"],
                        eligibility_decision=provenance["decision"] or "missing",
                    )
                )
        finding_types = _finding_type_counts(findings)
        return {
            "status": "ok",
            "summary": {
                "active_patterns": len(active),
                "disabled_patterns": len(disabled),
                "findings": len(findings),
                "finding_types": finding_types,
            },
            "findings": findings[:limit],
            "patterns": active[: min(limit, 50)],
            "disabled_patterns": disabled[: min(limit, 25)],
        }


_store: RoutePatternStore | None = None


def get_route_pattern_store() -> RoutePatternStore:
    global _store
    if _store is None:
        _store = RoutePatternStore()
    return _store
