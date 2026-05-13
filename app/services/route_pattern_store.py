from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any

_DB_PATH = Path("qdrant_data") / "route_patterns.db"

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


class RoutePatternStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()

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


_store: RoutePatternStore | None = None


def get_route_pattern_store() -> RoutePatternStore:
    global _store
    if _store is None:
        _store = RoutePatternStore()
    return _store
