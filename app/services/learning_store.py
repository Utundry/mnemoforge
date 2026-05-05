"""
Learning Store — SQLite-backed store for adaptive learning artifacts.

Schema (learning.db):
  artifacts  — hints / if_then_rules / meta_guidance with full lifecycle
  events     — append-only canonical learning events
  feedback   — explicit and implicit feedback signals

Artifact scope lifecycle:
  candidate → runtime_hint → persistent_rule → promoted_pattern

Candidate flow (human-in-the-loop):
  1. Miner (rule-based or GLM) calls upsert_candidate()
  2. If key already exists → evidence_count++, no duplicate row
  3. GET /learning/report surfaces candidates where
     evidence_count >= min_evidence and next_surface_after <= now
  4. Human approves / rejects / defers
  5. Only approve() promotes scope to runtime_hint (status=active)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

_DB_PATH = Path("qdrant_data") / "learning.db"

_ARTIFACT_SCOPE_ORDER = ["candidate", "runtime_hint", "persistent_rule", "promoted_pattern"]

# Minimum evidence_count per action_type before a candidate surfaces in /learning/report
EVIDENCE_THRESHOLDS: dict[str, int] = {
    "auto_save_result":         5,
    "suggest_save_result":      3,
    "run_tests":                4,
    "suggest_run_tests":        3,
    "create_improvement":       3,
    "suggest_create_improvement": 3,
    "rebuild_docs":             4,
    "suggest_rebuild_docs":     3,
    "request_missing_info":     3,
    "switch_to_background_job": 4,
    "_default":                 3,
}

# Maximum defer wait (days); after this the candidate is auto-archived
_DEFER_MAX_DAYS = 90
_DEFER_BASE_DAYS = 7

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS artifacts (
    id                TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL DEFAULT '',
    artifact_type     TEXT NOT NULL DEFAULT 'workflow_guidance',
    artifact_scope    TEXT NOT NULL DEFAULT 'runtime_hint',
    workflow_type     TEXT NOT NULL DEFAULT '',
    workflow_action   TEXT NOT NULL DEFAULT '',
    workflow_context  TEXT NOT NULL DEFAULT '',
    content           TEXT NOT NULL DEFAULT '',
    confidence        REAL NOT NULL DEFAULT 0.7,
    useful_votes      INTEGER NOT NULL DEFAULT 0,
    not_useful_votes  INTEGER NOT NULL DEFAULT 0,
    promoted_by       TEXT,
    tags              TEXT NOT NULL DEFAULT '[]',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    episode_id        TEXT NOT NULL DEFAULT '',
    agent_id          TEXT NOT NULL DEFAULT '',
    project           TEXT NOT NULL DEFAULT '',
    transport         TEXT NOT NULL DEFAULT '',
    event_type        TEXT NOT NULL,
    context_signature TEXT NOT NULL DEFAULT '',
    payload_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    episode_id   TEXT NOT NULL DEFAULT '',
    artifact_id  TEXT,
    valence      TEXT NOT NULL,
    magnitude    REAL NOT NULL DEFAULT 0.5,
    source       TEXT NOT NULL DEFAULT 'user',
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_artifacts_agent      ON artifacts(agent_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type       ON artifacts(artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_scope      ON artifacts(artifact_scope);
CREATE INDEX IF NOT EXISTS idx_artifacts_wf_type    ON artifacts(workflow_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_created    ON artifacts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_ts            ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_agent         ON events(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_context       ON events(context_signature);
CREATE INDEX IF NOT EXISTS idx_events_type          ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_feedback_ts          ON feedback(ts DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_artifact    ON feedback(artifact_id);
"""

# Columns added after initial schema — applied via ALTER TABLE migration
_NEW_ARTIFACT_COLUMNS: dict[str, str] = {
    "domain":             "TEXT DEFAULT ''",
    "action_type":        "TEXT DEFAULT ''",
    "key":                "TEXT DEFAULT ''",
    "risk_level":         "TEXT DEFAULT 'low'",
    "context_signature":  "TEXT DEFAULT ''",
    "trigger_dsl":        "TEXT DEFAULT ''",
    "evidence_count":     "INTEGER DEFAULT 0",
    "accepts":            "INTEGER DEFAULT 0",
    "rejects":            "INTEGER DEFAULT 0",
    "cooldown_s":         "INTEGER DEFAULT 1800",
    "last_emitted_ts":    "REAL DEFAULT 0",
    "status":             "TEXT DEFAULT 'active'",
    "next_surface_after": "REAL DEFAULT 0",
    "defer_count":        "INTEGER DEFAULT 0",
    "observation":        "TEXT DEFAULT ''",
    "why_it_matters":     "TEXT DEFAULT ''",
    "meta_json":          "TEXT DEFAULT '{}'",
}

_NEW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_artifacts_key     ON artifacts(key)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_status  ON artifacts(status)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_context ON artifacts(context_signature)",
]

_UNIQUE_KEY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_key_uniq_nonempty "
    "ON artifacts(key) WHERE key != ''"
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["meta"] = json.loads(d.get("meta_json") or "{}")
    return d


def make_context_signature(
    *,
    project: str = "unknown",
    task_type: str = "unknown",
    phase: str = "unknown",
    category: str = "unknown",
    transport: str = "unknown",
    agent: str | None = None,
) -> str:
    """Deterministic context key used for dedup, throttle, and ledger_mirror lookup."""
    parts: dict[str, str] = {
        "category":  category  or "unknown",
        "phase":     phase     or "unknown",
        "project":   project   or "unknown",
        "task_type": task_type or "unknown",
        "transport": transport or "unknown",
    }
    if agent:
        parts["agent"] = agent
    return ";".join(f"{k}={v}" for k, v in sorted(parts.items()))


def _normalize_trigger(trigger: str) -> str:
    """Normalize DSL trigger string for dedup key: sort conditions, strip, lowercase."""
    if not trigger:
        return ""
    conditions = [c.strip().lower() for c in re.split(r"\band\b", trigger, flags=re.IGNORECASE)]
    return " and ".join(sorted(conditions))


def make_artifact_key(action_type: str, trigger: str, context_signature: str) -> str:
    """
    Deterministic dedup key.
    Format: action_type::normalize_trigger(trigger)::context_signature
    Two candidates with the same key are considered duplicates.
    """
    normalized = _normalize_trigger(trigger)
    raw = f"{action_type}::{normalized}::{context_signature}"
    # Use SHA-256 prefix to keep key length bounded and safe for DB index
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{action_type}::{digest}"


def min_evidence_for(action_type: str) -> int:
    return EVIDENCE_THRESHOLDS.get(action_type, EVIDENCE_THRESHOLDS["_default"])


# ── Semantic dedup (best-effort, no embeddings) ────────────────────────────────

_SEMANTIC_DEDUP_ENABLED = os.getenv("LEARNING_SEMANTIC_DEDUP", "1") not in {"0", "false", "False"}
_SEMANTIC_DEDUP_SCAN_LIMIT = int(os.getenv("LEARNING_SEMANTIC_DEDUP_SCAN_LIMIT", "250"))
_SEMANTIC_DEDUP_JACCARD = float(os.getenv("LEARNING_SEMANTIC_DEDUP_JACCARD", "0.86"))
_SEMANTIC_DEDUP_MAX_HAMMING = int(os.getenv("LEARNING_SEMANTIC_DEDUP_MAX_HAMMING", "4"))

_VECTOR_DEDUP_ENABLED = os.getenv("LEARNING_VECTOR_DEDUP", "1") not in {"0", "false", "False"}
_VECTOR_DEDUP_THRESHOLD = float(os.getenv("LEARNING_VECTOR_DEDUP_THRESHOLD", "0.93"))
_VECTOR_DEDUP_LIMIT = int(os.getenv("LEARNING_VECTOR_DEDUP_LIMIT", "5"))


def _semantic_normalize(text: str) -> str:
    """Cheap normalization for near-duplicate detection (language-agnostic)."""
    t = (text or "").lower()
    t = re.sub(r"[\r\n\t]+", " ", t)
    # Keep letters/numbers/underscore; collapse everything else to spaces.
    t = re.sub(r"[^\w]+", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def _semantic_tokens(text: str) -> list[str]:
    # Drop very short tokens to reduce noise.
    return [t for t in _semantic_normalize(text).split(" ") if len(t) >= 3]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _simhash64(tokens: list[str]) -> int:
    """
    64-bit SimHash over tokens.

    This is not true semantic understanding, but it reliably catches repeated
    rules/hints that differ only by punctuation, casing, or small wording changes.
    """
    if not tokens:
        return 0
    weights: dict[str, int] = {}
    for t in tokens:
        weights[t] = weights.get(t, 0) + 1

    v = [0] * 64
    for t, w in weights.items():
        h = int(hashlib.sha1(t.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(64):
            v[i] += w if ((h >> i) & 1) else -w
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out


def _hamming64(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


def _merge_meta(existing: dict, incoming: dict) -> dict:
    """
    Merge incoming meta into existing meta.

    - list fields: union (stable order)
    - scalar fields: keep existing unless missing/empty
    """
    if not incoming:
        return existing or {}
    out = dict(existing or {})
    for k, v in incoming.items():
        if v is None:
            continue
        if isinstance(v, list):
            prev = out.get(k)
            merged: set = set(prev) if isinstance(prev, list) else set()
            for item in v:
                merged.add(item)
            out[k] = sorted(merged, key=lambda x: str(x))
            continue
        prev = out.get(k)
        if prev is None or (isinstance(prev, str) and not prev.strip()):
            out[k] = v
    return out


# ── Store ──────────────────────────────────────────────────────────────────────

def _merge_text(existing: str, incoming: str, *, max_len: int = 4000) -> str:
    """
    Best-effort enrichment merge for human-readable fields (observation, why_it_matters).

    - Keep existing text
    - Append new text only if it's non-empty and not already contained
    - Cap the resulting length to avoid unbounded growth in UI payloads
    """
    existing_text = (existing or "").strip()
    incoming_text = (incoming or "").strip()
    if not incoming_text:
        return existing or ""
    if not existing_text:
        return incoming_text[:max_len]
    if incoming_text in existing_text:
        return existing_text

    merged = f"{existing_text}\n\n{incoming_text}"
    if len(merged) <= max_len:
        return merged
    return (merged[: max(0, max_len - 1)].rstrip() + "…")


@dataclass(slots=True)
class _WriteCmd:
    op: str
    args: dict
    future: Optional[asyncio.Future[int]] = None


class LearningStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="learningdb")
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA busy_timeout=2000")
            except Exception:
                pass
            self._conn.executescript(_CREATE_SQL)
            self._conn.commit()
            self._migrate_schema()

        # Async write-behind buffer for high-volume writes (events/feedback)
        self._writer_start_lock: Optional[asyncio.Lock] = None
        self._write_q: Optional[asyncio.Queue[_WriteCmd]] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._writer_stop: Optional[asyncio.Event] = None
        logger.info("LearningStore initialized: %s", db_path)

    def _migrate_schema(self) -> None:
        """Add new columns to artifacts table if missing; create new indexes."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }
        for col, typedef in _NEW_ARTIFACT_COLUMNS.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE artifacts ADD COLUMN {col} {typedef}")
                logger.info("LearningStore: added column artifacts.%s", col)
        # Best-effort cleanup of historical duplicate keys before adding a UNIQUE index.
        self._dedupe_duplicate_keys_by_key()
        for stmt in _NEW_INDEXES:
            self._conn.execute(stmt)
        try:
            self._conn.execute(_UNIQUE_KEY_INDEX)
        except sqlite3.IntegrityError:
            # If something inserted duplicates concurrently during migration, dedupe and retry once.
            self._dedupe_duplicate_keys_by_key()
            self._conn.execute(_UNIQUE_KEY_INDEX)
        self._conn.commit()

    def _dedupe_duplicate_keys_by_key(self) -> int:
        """
        Collapse duplicate artifact rows that share the same non-empty `key`.

        This protects the Learning Ledger from repeated "rules" when multiple processes
        (or overlapping runs) insert the same artifact concurrently. With only code-level
        checks, SQLite can still accumulate duplicates without a DB-level UNIQUE index.
        """
        def _scope_rank(scope: str) -> int:
            try:
                return _ARTIFACT_SCOPE_ORDER.index(scope)
            except ValueError:
                return -1

        def _status_rank(status: str) -> int:
            return {
                "active": 3,
                "pending_review": 2,
                "archived": 1,
                "disabled": 0,
            }.get(status, 0)

        def _risk_rank(risk: str) -> int:
            return {"low": 0, "medium": 1, "high": 2}.get(risk, 0)

        dup_keys = self._conn.execute(
            "SELECT key FROM artifacts WHERE key != '' GROUP BY key HAVING COUNT(*) > 1"
        ).fetchall()
        if not dup_keys:
            return 0

        deduped = 0
        for r in dup_keys:
            key = r[0]
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE key = ? ORDER BY updated_at DESC",
                (key,),
            ).fetchall()
            if len(rows) <= 1:
                continue

            items = [dict(row) for row in rows]
            winner = max(
                items,
                key=lambda d: (
                    _scope_rank(str(d.get("artifact_scope") or "")),
                    _status_rank(str(d.get("status") or "")),
                    float(d.get("updated_at") or 0),
                ),
            )
            winner_id = str(winner["id"])
            loser_ids = [str(d["id"]) for d in items if str(d["id"]) != winner_id]
            if not loser_ids:
                continue

            def _sum_int(field: str) -> int:
                total = 0
                for d in items:
                    try:
                        total += int(d.get(field) or 0)
                    except Exception:
                        pass
                return total

            def _max_float(field: str) -> float:
                vals: list[float] = []
                for d in items:
                    try:
                        vals.append(float(d.get(field) or 0))
                    except Exception:
                        pass
                return max(vals) if vals else 0.0

            def _min_float(field: str) -> float:
                vals: list[float] = []
                for d in items:
                    try:
                        vals.append(float(d.get(field) or 0))
                    except Exception:
                        pass
                return min(vals) if vals else 0.0

            def _first_nonempty(field: str, default: str = "") -> str:
                for d in items:
                    v = d.get(field)
                    if isinstance(v, str) and v.strip():
                        return v
                return default

            def _merge_tags() -> str:
                merged: set[str] = set()
                for d in items:
                    raw = d.get("tags") or "[]"
                    try:
                        arr = json.loads(raw) if isinstance(raw, str) else (raw or [])
                    except Exception:
                        arr = []
                    for t in arr if isinstance(arr, list) else []:
                        if isinstance(t, str) and t.strip():
                            merged.add(t.strip())
                return json.dumps(sorted(merged))

            merged_scope = max(
                items, key=lambda d: _scope_rank(str(d.get("artifact_scope") or ""))
            ).get("artifact_scope", winner.get("artifact_scope"))
            merged_status = max(
                items, key=lambda d: _status_rank(str(d.get("status") or ""))
            ).get("status", winner.get("status"))
            merged_risk = max(items, key=lambda d: _risk_rank(str(d.get("risk_level") or ""))).get(
                "risk_level", winner.get("risk_level")
            )
            merged_context = winner.get("context_signature") or _first_nonempty("context_signature", "")
            merged_trigger = winner.get("trigger_dsl") or _first_nonempty("trigger_dsl", "")
            merged_obs = winner.get("observation") or _first_nonempty("observation", "")
            merged_why = winner.get("why_it_matters") or _first_nonempty("why_it_matters", "")

            self._conn.execute(
                """
                UPDATE artifacts
                   SET artifact_scope = ?,
                       status = ?,
                       risk_level = ?,
                       context_signature = ?,
                       trigger_dsl = ?,
                       observation = ?,
                       why_it_matters = ?,
                       confidence = ?,
                       evidence_count = ?,
                       useful_votes = ?,
                       not_useful_votes = ?,
                       accepts = ?,
                       rejects = ?,
                       defer_count = ?,
                       cooldown_s = ?,
                       last_emitted_ts = ?,
                       next_surface_after = ?,
                       created_at = ?,
                       updated_at = ?,
                       tags = ?
                 WHERE id = ?
                """,
                (
                    str(merged_scope or winner.get("artifact_scope") or "runtime_hint"),
                    str(merged_status or winner.get("status") or "active"),
                    str(merged_risk or winner.get("risk_level") or "low"),
                    str(merged_context or ""),
                    str(merged_trigger or ""),
                    str(merged_obs or ""),
                    str(merged_why or ""),
                    max(_max_float("confidence"), float(winner.get("confidence") or 0.7)),
                    _sum_int("evidence_count"),
                    _sum_int("useful_votes"),
                    _sum_int("not_useful_votes"),
                    _sum_int("accepts"),
                    _sum_int("rejects"),
                    _sum_int("defer_count"),
                    max(int(d.get("cooldown_s") or 0) for d in items),
                    _max_float("last_emitted_ts"),
                    _max_float("next_surface_after"),
                    _min_float("created_at") or float(winner.get("created_at") or 0),
                    _max_float("updated_at") or float(winner.get("updated_at") or 0),
                    _merge_tags(),
                    winner_id,
                ),
            )

            placeholders = ",".join("?" * len(loser_ids))
            try:
                self._conn.execute(
                    f"UPDATE feedback SET artifact_id = ? WHERE artifact_id IN ({placeholders})",
                    [winner_id] + loser_ids,
                )
            except Exception:
                pass

            self._conn.execute(
                f"DELETE FROM artifacts WHERE id IN ({placeholders})",
                loser_ids,
            )
            deduped += len(loser_ids)

        if deduped:
            logger.warning("LearningStore: deduped %s duplicate artifact rows by key", deduped)
        return deduped

    async def _ensure_writer(self) -> None:
        if self._writer_start_lock is None:
            self._writer_start_lock = asyncio.Lock()
        async with self._writer_start_lock:
            if self._writer_task is not None and not self._writer_task.done():
                return
            self._write_q = asyncio.Queue(maxsize=20000)
            self._writer_stop = asyncio.Event()
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def aclose(self) -> None:
        # Stop writer loop first so no more writes race with close.
        if (
            self._writer_task is not None
            and self._writer_stop is not None
            and self._write_q is not None
        ):
            self._writer_stop.set()
            try:
                await asyncio.wait_for(self._write_q.join(), timeout=10.0)
                await asyncio.wait_for(self._writer_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._writer_task.cancel()
                try:
                    await self._writer_task
                except asyncio.CancelledError:
                    pass
        self._writer_task = None
        self._writer_stop = None
        self._write_q = None

        # Close DB and executor
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
        self._executor.shutdown(wait=True, cancel_futures=False)

    async def _run_sync(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    async def _writer_loop(self) -> None:
        assert self._write_q is not None
        assert self._writer_stop is not None

        max_batch = 500
        max_delay_s = 0.10

        while True:
            if self._writer_stop.is_set() and self._write_q.empty():
                break
            try:
                first = await asyncio.wait_for(self._write_q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

            batch = [first]
            t0 = time.time()
            while len(batch) < max_batch and (time.time() - t0) < max_delay_s:
                try:
                    batch.append(self._write_q.get_nowait())
                except asyncio.QueueEmpty:
                    break

            try:
                ids = await self._run_sync(self._flush_write_cmds_sync, batch)
                for cmd, row_id in zip(batch, ids):
                    if cmd.future is not None and not cmd.future.done():
                        cmd.future.set_result(row_id)
            except Exception as e:
                for cmd in batch:
                    if cmd.future is not None and not cmd.future.done():
                        cmd.future.set_exception(e)
            finally:
                for _ in batch:
                    self._write_q.task_done()

    def _flush_write_cmds_sync(self, batch: list["_WriteCmd"]) -> list[int]:
        ids: list[int] = []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                for cmd in batch:
                    a = cmd.args
                    if cmd.op == "event":
                        cur.execute(
                            """
                            INSERT INTO events
                                (ts, episode_id, agent_id, project, transport, event_type, context_signature, payload_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                float(a.get("ts") or time.time()),
                                a.get("episode_id", ""),
                                a.get("agent_id", ""),
                                a.get("project", ""),
                                a.get("transport", ""),
                                a["event_type"],
                                a.get("context_signature", ""),
                                json.dumps(a.get("payload") or {}),
                            ),
                        )
                        ids.append(int(cur.lastrowid))
                    elif cmd.op == "feedback":
                        cur.execute(
                            """
                            INSERT INTO feedback
                                (ts, episode_id, artifact_id, valence, magnitude, source, payload_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                float(a.get("ts") or time.time()),
                                a.get("episode_id", ""),
                                a.get("artifact_id"),
                                a["valence"],
                                float(a.get("magnitude", 0.5)),
                                a.get("source", "user"),
                                json.dumps(a.get("payload") or {}),
                            ),
                        )
                        ids.append(int(cur.lastrowid))
                    else:
                        raise RuntimeError(f"Unknown write op: {cmd.op}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return ids

    # ── Events ────────────────────────────────────────────────────────────────

    async def write_event(
        self,
        *,
        event_type: str,
        agent_id: str = "",
        project: str = "",
        transport: str = "mcp",
        episode_id: str = "",
        context_signature: str = "",
        payload: dict | None = None,
    ) -> int:
        """Append a canonical learning event. Returns the row id."""
        await self._ensure_writer()
        assert self._write_q is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        cmd = _WriteCmd(
            op="event",
            args={
                "ts": time.time(),
                "episode_id": episode_id,
                "agent_id": agent_id,
                "project": project,
                "transport": transport,
                "event_type": event_type,
                "context_signature": context_signature,
                "payload": payload or {},
            },
            future=fut,
        )
        await self._write_q.put(cmd)
        return await fut

    async def list_events(
        self,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        episode_id: Optional[str] = None,
        context_signature: Optional[str] = None,
        since_ts: Optional[float] = None,
        before_ts: Optional[float] = None,
        before_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        if context_signature:
            clauses.append("context_signature = ?")
            params.append(context_signature)
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if before_ts is not None:
            if before_id is not None:
                clauses.append("(ts < ? OR (ts = ? AND id < ?))")
                params.extend([before_ts, before_ts, int(before_id)])
            else:
                clauses.append("ts < ?")
                params.append(before_ts)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        def _do() -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?",
                    params,
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._run_sync(_do)

    async def count_events(
        self,
        event_type: str,
        context_signature: str,
        since_ts: float,
    ) -> int:
        """Count events matching type + context within a time window."""

        def _do() -> int:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = ? AND context_signature = ? AND ts >= ?",
                    (event_type, context_signature, since_ts),
                ).fetchone()
            return int(row[0]) if row else 0

        return await self._run_sync(_do)

    # ── Feedback ──────────────────────────────────────────────────────────────

    async def write_feedback(
        self,
        *,
        valence: str,
        episode_id: str = "",
        artifact_id: Optional[str] = None,
        magnitude: float = 0.5,
        source: str = "user",
        payload: dict | None = None,
    ) -> int:
        """Record a feedback signal. valence: 'positive' | 'negative'."""
        await self._ensure_writer()
        assert self._write_q is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        cmd = _WriteCmd(
            op="feedback",
            args={
                "ts": time.time(),
                "episode_id": episode_id,
                "artifact_id": artifact_id,
                "valence": valence,
                "magnitude": magnitude,
                "source": source,
                "payload": payload or {},
            },
            future=fut,
        )
        await self._write_q.put(cmd)
        return await fut

    async def list_feedback(
        self,
        artifact_id: Optional[str] = None,
        source: Optional[str] = None,
        since_ts: Optional[float] = None,
        limit: int = 50,
    ) -> list[dict]:
        params: list = []
        clauses = []
        if artifact_id:
            clauses.append("artifact_id = ?")
            params.append(artifact_id)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(float(since_ts))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        def _do() -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM feedback {where} ORDER BY ts DESC LIMIT ?",
                    params,
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._run_sync(_do)

    # ── Artifacts — write ─────────────────────────────────────────────────────

    async def insert_artifact(
        self,
        *,
        agent_id: str,
        artifact_type: str = "workflow_guidance",
        workflow_type: str = "",
        workflow_action: str = "",
        workflow_context: str = "",
        content: str = "",
        confidence: float = 0.7,
        tags: list[str] | None = None,
        artifact_id: Optional[UUID] = None,
        created_at: Optional[float] = None,
        # Extended fields
        domain: str = "",
        action_type: str = "",
        key: str = "",
        risk_level: str = "low",
        context_signature: str = "",
        trigger_dsl: str = "",
        evidence_count: int = 0,
        cooldown_s: int = 1800,
        status: str = "active",
        scope: str = "runtime_hint",
        observation: str = "",
        why_it_matters: str = "",
    ) -> UUID:
        """Insert a new artifact. For candidates use upsert_candidate() instead."""
        uid = artifact_id or uuid4()
        now = created_at if created_at is not None else time.time()

        def _do() -> None:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO artifacts
                        (id, agent_id, artifact_type, artifact_scope,
                         workflow_type, workflow_action, workflow_context,
                         content, confidence, useful_votes, not_useful_votes,
                         tags, created_at, updated_at,
                         domain, action_type, key, risk_level, context_signature,
                         trigger_dsl, evidence_count, cooldown_s, status,
                         observation, why_it_matters)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uid), agent_id, artifact_type, scope,
                        workflow_type, workflow_action, workflow_context,
                        content, confidence,
                        json.dumps(tags or []), now, now,
                        domain, action_type, key, risk_level, context_signature,
                        trigger_dsl, evidence_count, cooldown_s, status,
                        observation, why_it_matters,
                    ),
                )
                self._conn.commit()

        await self._run_sync(_do)
        return uid

    async def upsert_candidate(
        self,
        *,
        agent_id: str,
        action_type: str,
        content: str,
        trigger_dsl: str = "",
        context_signature: str = "",
        observation: str = "",
        why_it_matters: str = "",
        risk_level: str = "low",
        confidence: float = 0.7,
        tags: list[str] | None = None,
        domain: str = "",
        artifact_type: str = "hint",
        meta: dict | None = None,
    ) -> tuple[UUID, bool]:
        """
        Insert or increment evidence for a candidate artifact.

        Dedup: candidates sharing the same key (action_type + trigger + context_signature)
        are collapsed into one row — evidence_count is incremented instead of inserting a dupe.

        Returns (artifact_id, created: bool).
        """
        key = make_artifact_key(action_type, trigger_dsl, context_signature)
        now = time.time()

        _REJECT_COOLDOWN_S = 14 * 86400  # rejected candidates stay suppressed for 14 days

        def _merge_existing(*, row, bump_by: int = 1) -> tuple[UUID, bool]:
            new_count = int(row["evidence_count"] or 0) + bump_by
            try:
                existing_meta = json.loads(row["meta_json"] or "{}")
            except Exception:
                existing_meta = {}
            merged_meta = _merge_meta(existing_meta, meta or {})

            # Preserve alternative formulations / extra details from repeated observations.
            existing_content = (row["content"] or "").strip()
            incoming_content = (content or "").strip()
            if incoming_content and existing_content and incoming_content != existing_content:
                variants = merged_meta.get("content_variants")
                if not isinstance(variants, list):
                    variants = []
                if incoming_content not in variants:
                    variants.append(incoming_content[:1200])
                merged_meta["content_variants"] = variants[-8:]

            new_observation = _merge_text(row["observation"] or "", observation or "")
            new_why = _merge_text(row["why_it_matters"] or "", why_it_matters or "")

            try:
                existing_tags = json.loads(row["tags"] or "[]")
            except Exception:
                existing_tags = []
            merged_tags = sorted({*(existing_tags if isinstance(existing_tags, list) else []), *((tags or []))})
            self._conn.execute(
                "UPDATE artifacts SET evidence_count = ?, updated_at = ?, meta_json = ?, tags = ?, observation = ?, why_it_matters = ? WHERE id = ?",
                (new_count, now, json.dumps(merged_meta), json.dumps(merged_tags), new_observation, new_why, row["id"]),
            )
            self._conn.commit()
            return UUID(row["id"]), False

        def _try_dedup_without_insert() -> Optional[tuple[UUID, bool]]:
            with self._lock:
                # Check for existing candidate with same key (pending review)
                row = self._conn.execute(
                    """
                    SELECT id, evidence_count, tags, meta_json, content, observation, why_it_matters FROM artifacts
                    WHERE key = ? AND artifact_scope = 'candidate'
                      AND status NOT IN ('archived', 'disabled')
                    """,
                    (key,),
                ).fetchone()

                if row:
                    return _merge_existing(row=row, bump_by=1)

                # Don't recreate if already promoted to active (approved)
                active_row = self._conn.execute(
                    "SELECT id FROM artifacts WHERE key = ? AND status = 'active'",
                    (key,),
                ).fetchone()
                if active_row:
                    return UUID(active_row["id"]), False

                # Don't recreate if recently rejected (within 14 days)
                recent_reject = self._conn.execute(
                    """SELECT id FROM artifacts
                       WHERE key = ? AND status = 'archived' AND updated_at > ?""",
                    (key, now - _REJECT_COOLDOWN_S),
                ).fetchone()
                if recent_reject:
                    return UUID(recent_reject["id"]), False

                # ── Semantic dedup (action-scoped, ignores context_signature) ──
                # This prevents repeated candidates when the only difference is the context_signature
                # (e.g., different project/task) but the rule/hint text is effectively the same.
                if _SEMANTIC_DEDUP_ENABLED:
                    semantic_text = f"{trigger_dsl}\n{content}".strip()
                    norm = _semantic_normalize(semantic_text)
                    norm_sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()
                    new_tokens = _semantic_tokens(semantic_text)
                    new_set = set(new_tokens)
                    new_sim = _simhash64(new_tokens)

                    scan = self._conn.execute(
                        """
                        SELECT id, evidence_count, content, trigger_dsl, tags, meta_json
                          FROM artifacts
                         WHERE action_type = ?
                           AND artifact_scope = 'candidate'
                           AND status NOT IN ('archived', 'disabled')
                         ORDER BY updated_at DESC
                         LIMIT ?
                        """,
                        (action_type, _SEMANTIC_DEDUP_SCAN_LIMIT),
                    ).fetchall()

                    best_id: Optional[str] = None
                    best_evidence: int = 0
                    best_tags_raw: str = "[]"
                    best_meta_raw: str = "{}"
                    best_score: float = 0.0

                    for r in scan or []:
                        existing_text = f"{r['trigger_dsl'] or ''}\n{r['content'] or ''}".strip()
                        existing_norm = _semantic_normalize(existing_text)
                        existing_sha = hashlib.sha256(existing_norm.encode("utf-8")).hexdigest()
                        if existing_sha == norm_sha:
                            best_id = r["id"]
                            best_evidence = int(r["evidence_count"] or 0)
                            best_tags_raw = r["tags"] or "[]"
                            best_meta_raw = r["meta_json"] or "{}"
                            best_score = 1.0
                            break

                        existing_tokens = _semantic_tokens(existing_text)
                        existing_set = set(existing_tokens)
                        j = _jaccard(new_set, existing_set)
                        if j < (_SEMANTIC_DEDUP_JACCARD - 0.12):
                            continue
                        ham = _hamming64(new_sim, _simhash64(existing_tokens))

                        is_dup = (j >= _SEMANTIC_DEDUP_JACCARD) or (
                            ham <= _SEMANTIC_DEDUP_MAX_HAMMING and j >= (_SEMANTIC_DEDUP_JACCARD - 0.06)
                        )
                        if not is_dup:
                            continue

                        if j > best_score:
                            best_id = r["id"]
                            best_evidence = int(r["evidence_count"] or 0)
                            best_tags_raw = r["tags"] or "[]"
                            best_meta_raw = r["meta_json"] or "{}"
                            best_score = j

                    if best_id:
                        full = self._conn.execute(
                            "SELECT content, observation, why_it_matters FROM artifacts WHERE id = ?",
                            (best_id,),
                        ).fetchone()
                        existing_content = (full["content"] or "").strip() if full else ""
                        existing_observation = full["observation"] if full else ""
                        existing_why = full["why_it_matters"] if full else ""

                        try:
                            existing_meta = json.loads(best_meta_raw or "{}")
                        except Exception:
                            existing_meta = {}
                        merged_meta = _merge_meta(existing_meta, meta or {})
                        # Add semantic diagnostics for later debugging (non-authoritative)
                        merged_meta.setdefault("semantic_norm_sha256", norm_sha)
                        merged_meta.setdefault("semantic_simhash64", str(new_sim))

                        incoming_content = (content or "").strip()
                        if incoming_content and existing_content and incoming_content != existing_content:
                            variants = merged_meta.get("content_variants")
                            if not isinstance(variants, list):
                                variants = []
                            if incoming_content not in variants:
                                variants.append(incoming_content[:1200])
                            merged_meta["content_variants"] = variants[-8:]

                        new_observation = _merge_text(existing_observation or "", observation or "")
                        new_why = _merge_text(existing_why or "", why_it_matters or "")

                        try:
                            existing_tags = json.loads(best_tags_raw or "[]")
                        except Exception:
                            existing_tags = []
                        merged_tags = sorted({*(existing_tags if isinstance(existing_tags, list) else []), *((tags or []))})

                        self._conn.execute(
                            "UPDATE artifacts SET evidence_count = ?, updated_at = ?, meta_json = ?, tags = ?, observation = ?, why_it_matters = ? WHERE id = ?",
                            (best_evidence + 1, now, json.dumps(merged_meta), json.dumps(merged_tags), new_observation, new_why, best_id),
                        )
                        self._conn.commit()
                        return UUID(best_id), False

            return None

        def _increment_candidate_by_id(candidate_id: UUID, *, score: float | None = None) -> Optional[tuple[UUID, bool]]:
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT id, evidence_count, tags, meta_json, content, observation, why_it_matters FROM artifacts
                    WHERE id = ? AND artifact_scope = 'candidate'
                      AND status NOT IN ('archived', 'disabled')
                    """,
                    (str(candidate_id),),
                ).fetchone()
                if row is None:
                    return None

                # Inject vector-dedup diagnostics
                try:
                    existing_meta = json.loads(row["meta_json"] or "{}")
                except Exception:
                    existing_meta = {}
                merged_meta = _merge_meta(existing_meta, meta or {})
                if score is not None:
                    merged_meta["vector_dedup_last_score"] = float(score)
                merged_meta["vector_dedup_last_ts"] = now

                existing_content = (row["content"] or "").strip()
                incoming_content = (content or "").strip()
                if incoming_content and existing_content and incoming_content != existing_content:
                    variants = merged_meta.get("content_variants")
                    if not isinstance(variants, list):
                        variants = []
                    if incoming_content not in variants:
                        variants.append(incoming_content[:1200])
                    merged_meta["content_variants"] = variants[-8:]

                new_observation = _merge_text(row["observation"] or "", observation or "")
                new_why = _merge_text(row["why_it_matters"] or "", why_it_matters or "")

                try:
                    existing_tags = json.loads(row["tags"] or "[]")
                except Exception:
                    existing_tags = []
                merged_tags = sorted({*(existing_tags if isinstance(existing_tags, list) else []), *((tags or []))})

                new_count = int(row["evidence_count"] or 0) + 1
                self._conn.execute(
                    "UPDATE artifacts SET evidence_count = ?, updated_at = ?, meta_json = ?, tags = ?, observation = ?, why_it_matters = ? WHERE id = ?",
                    (new_count, now, json.dumps(merged_meta), json.dumps(merged_tags), new_observation, new_why, row["id"]),
                )
                self._conn.commit()
                return UUID(row["id"]), False

        def _insert_new_candidate(*, meta_out: dict) -> tuple[UUID, bool]:
            with self._lock:
                uid = uuid4()
                try:
                    self._conn.execute(
                        """
                        INSERT INTO artifacts
                            (id, agent_id, artifact_type, artifact_scope,
                             workflow_type, workflow_action, workflow_context,
                             content, confidence, useful_votes, not_useful_votes,
                             tags, created_at, updated_at,
                             domain, action_type, key, risk_level, context_signature,
                             trigger_dsl, evidence_count, status,
                             observation, why_it_matters, meta_json)
                        VALUES (?, ?, ?, 'candidate',
                                '', '', '',
                                ?, ?, 0, 0,
                                ?, ?, ?,
                                ?, ?, ?, ?, ?,
                                ?, 1, 'pending_review',
                                ?, ?, ?)
                        """,
                        (
                            str(uid), agent_id, artifact_type,
                            content, confidence,
                            json.dumps(tags or []), now, now,
                            domain, action_type, key, risk_level, context_signature,
                            trigger_dsl,
                            observation, why_it_matters, json.dumps(meta_out),
                        ),
                    )
                    self._conn.commit()
                    return uid, True
                except sqlite3.IntegrityError:
                    # Another process inserted the same key concurrently (UNIQUE index).
                    row2 = self._conn.execute(
                        """
                        SELECT id, evidence_count, artifact_scope, status FROM artifacts
                        WHERE key = ? AND status NOT IN ('disabled')
                        """,
                        (key,),
                    ).fetchone()
                    if row2:
                        # If it's still a candidate, increment evidence; otherwise treat as already satisfied.
                        if (row2["artifact_scope"] == "candidate") and (row2["status"] != "archived"):
                            new_count = int(row2["evidence_count"] or 0) + 1
                            self._conn.execute(
                                "UPDATE artifacts SET evidence_count = ?, updated_at = ? WHERE id = ?",
                                (new_count, now, row2["id"]),
                            )
                            self._conn.commit()
                        return UUID(row2["id"]), False
                    raise

        # Phase 1: cheap DB-only dedup (exact key + token/simhash)
        hit = await self._run_sync(_try_dedup_without_insert)
        if hit is not None:
            return hit

        # Phase 2: true semantic dedup via embeddings + Qdrant (best-effort)
        semantic_text = f"{trigger_dsl}\n{content}".strip()
        vector: Optional[list[float]] = None
        if _VECTOR_DEDUP_ENABLED and semantic_text:
            try:
                from app.dependencies import get_ollama, get_qdrant
                from app.services.embedding_gateway import embed_text
                from app.services.learning_vector_index import search_similar_candidates, upsert_artifact_vector

                qdrant = get_qdrant()
                ollama = get_ollama()
                vector, _embedding_meta = await embed_text(
                    semantic_text,
                    primary=ollama,
                    purpose="learning_candidate_dedup",
                    fallback_reason="learning_candidate_dedup_embedding_unavailable",
                )

                matches = await search_similar_candidates(
                    qdrant._client,
                    vector=vector,
                    action_type=action_type,
                    limit=_VECTOR_DEDUP_LIMIT,
                )
                best = None
                for mid, score, _pl in matches:
                    if score >= _VECTOR_DEDUP_THRESHOLD and (best is None or score > best[1]):
                        best = (mid, score)

                if best is not None:
                    updated = await self._run_sync(_increment_candidate_by_id, best[0], score=best[1])
                    if updated is not None:
                        return updated

            except Exception as exc:
                logger.debug("Vector semantic dedup skipped (non-fatal): %s", exc)

        # Phase 3: insert new candidate (and index its vector if available)
        meta_out = dict(meta or {})
        if _SEMANTIC_DEDUP_ENABLED and semantic_text:
            norm = _semantic_normalize(semantic_text)
            meta_out.setdefault("semantic_norm_sha256", hashlib.sha256(norm.encode("utf-8")).hexdigest())
            meta_out.setdefault("semantic_simhash64", str(_simhash64(_semantic_tokens(semantic_text))))
        if vector is not None:
            meta_out.setdefault("vector_indexed", True)
        uid, created = await self._run_sync(_insert_new_candidate, meta_out=meta_out)
        if created and vector is not None:
            try:
                from app.dependencies import get_qdrant
                from app.services.learning_vector_index import upsert_artifact_vector

                qdrant = get_qdrant()
                await upsert_artifact_vector(
                    qdrant._client,
                    artifact_id=uid,
                    vector=vector,
                    payload={
                        "artifact_scope": "candidate",
                        "status": "pending_review",
                        "action_type": action_type,
                        "updated_at": now,
                    },
                )
            except Exception as exc:
                logger.debug("Learning vector index upsert skipped (non-fatal): %s", exc)

        return uid, created

    async def rate_artifact(self, artifact_id: UUID, useful: bool) -> Optional[dict]:
        """Increment vote counters, recompute confidence (Laplace +1). Returns updated row."""
        now = time.time()

        def _do() -> Optional[dict]:
            with self._lock:
                row = self._conn.execute(
                    "SELECT useful_votes, not_useful_votes FROM artifacts WHERE id = ?",
                    (str(artifact_id),),
                ).fetchone()
                if row is None:
                    return None
                useful_votes = row["useful_votes"] + (1 if useful else 0)
                not_useful_votes = row["not_useful_votes"] + (0 if useful else 1)
                total = useful_votes + not_useful_votes
                confidence = round(useful_votes / (total + 1), 4)
                self._conn.execute(
                    """UPDATE artifacts
                       SET useful_votes = ?, not_useful_votes = ?,
                           confidence = ?, updated_at = ?
                       WHERE id = ?""",
                    (useful_votes, not_useful_votes, confidence, now, str(artifact_id)),
                )
                self._conn.commit()
                updated = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
                ).fetchone()
            return _row_to_dict(updated) if updated else None

        return await self._run_sync(_do)

    async def promote_artifact(
        self,
        artifact_id: UUID,
        promoted_by: str,
        *,
        promotion_source: str = "inline_user_approval",
        promotion_reason: str = "",
    ) -> Optional[dict]:
        """Advance scope one step in lifecycle. Returns updated row or None if at max/candidate."""
        now = time.time()

        def _do() -> Optional[dict]:
            with self._lock:
                row = self._conn.execute(
                    "SELECT artifact_scope, meta_json FROM artifacts WHERE id = ?",
                    (str(artifact_id),),
                ).fetchone()
                if row is None:
                    return None
                current = row["artifact_scope"]
                # candidate must go through approve(), not promote()
                if current == "candidate":
                    return None
                try:
                    idx = _ARTIFACT_SCOPE_ORDER.index(current)
                    next_scope = _ARTIFACT_SCOPE_ORDER[idx + 1]
                except (ValueError, IndexError):
                    return None
                meta = json.loads(row["meta_json"] or "{}")
                meta["last_promoted_by"] = (promoted_by or "").strip()
                meta["last_promotion_source"] = (promotion_source or "inline_user_approval").strip() or "inline_user_approval"
                meta["last_promoted_at"] = now
                meta["last_promotion_from"] = current
                meta["last_promotion_to"] = next_scope
                if promotion_reason.strip():
                    meta["last_promotion_reason"] = promotion_reason.strip()
                self._conn.execute(
                    """UPDATE artifacts
                       SET artifact_scope = ?, promoted_by = ?, updated_at = ?, meta_json = ?
                       WHERE id = ?""",
                    (next_scope, promoted_by, now, json.dumps(meta), str(artifact_id)),
                )
                self._conn.commit()
                updated = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
                ).fetchone()
            return _row_to_dict(updated) if updated else None

        return await self._run_sync(_do)

    # ── Candidate review ──────────────────────────────────────────────────────

    async def approve_candidate(
        self,
        artifact_id: UUID,
        *,
        approved_by: str = "user",
        approval_source: str = "inline_user_approval",
        approval_reason: str = "",
    ) -> Optional[dict]:
        """Promote candidate → runtime_hint (status=active)."""
        now = time.time()

        def _do() -> Optional[dict]:
            with self._lock:
                row = self._conn.execute(
                    "SELECT artifact_scope, status, meta_json FROM artifacts WHERE id = ?",
                    (str(artifact_id),),
                ).fetchone()
                if (
                    row is None
                    or row["artifact_scope"] != "candidate"
                    or row["status"] != "pending_review"
                ):
                    return None
                meta = json.loads(row["meta_json"] or "{}")
                meta["approved_by"] = (approved_by or "user").strip() or "user"
                meta["approval_source"] = (approval_source or "inline_user_approval").strip() or "inline_user_approval"
                meta["approved_at"] = now
                if approval_reason.strip():
                    meta["approval_reason"] = approval_reason.strip()
                self._conn.execute(
                    """UPDATE artifacts
                       SET artifact_scope = 'runtime_hint',
                           status = 'active',
                           updated_at = ?,
                           meta_json = ?
                       WHERE id = ?""",
                    (now, json.dumps(meta), str(artifact_id)),
                )
                self._conn.commit()
                updated = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
                ).fetchone()
            return _row_to_dict(updated) if updated else None

        updated = await self._run_sync(_do)
        if updated is not None:
            try:
                from app.dependencies import get_qdrant
                from app.services.learning_vector_index import set_artifact_payload

                qdrant = get_qdrant()
                await set_artifact_payload(
                    qdrant._client,
                    artifact_id=artifact_id,
                    payload={
                        "artifact_scope": "runtime_hint",
                        "status": "active",
                        "updated_at": now,
                    },
                )
            except Exception as exc:
                logger.debug("Learning vector index payload update skipped (non-fatal): %s", exc)
        return updated

    async def reject_candidate(
        self,
        artifact_id: UUID,
        *,
        rejected_by: str = "user",
        rejection_source: str = "inline_user_approval",
        rejection_reason: str = "",
    ) -> Optional[dict]:
        """Archive candidate and record rejection."""
        now = time.time()

        def _do() -> Optional[dict]:
            with self._lock:
                row = self._conn.execute(
                    "SELECT artifact_scope, status, meta_json FROM artifacts WHERE id = ?",
                    (str(artifact_id),),
                ).fetchone()
                if (
                    row is None
                    or row["artifact_scope"] != "candidate"
                    or row["status"] != "pending_review"
                ):
                    return None
                meta = json.loads(row["meta_json"] or "{}")
                meta["rejected_by"] = (rejected_by or "user").strip() or "user"
                meta["rejection_source"] = (
                    (rejection_source or "inline_user_approval").strip() or "inline_user_approval"
                )
                meta["rejected_at"] = now
                if rejection_reason.strip():
                    meta["rejection_reason"] = rejection_reason.strip()
                self._conn.execute(
                    """UPDATE artifacts
                       SET status = 'archived', rejects = rejects + 1, updated_at = ?, meta_json = ?
                       WHERE id = ?""",
                    (now, json.dumps(meta), str(artifact_id)),
                )
                self._conn.commit()
                updated = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
                ).fetchone()
            return _row_to_dict(updated) if updated else None

        updated = await self._run_sync(_do)
        if updated is not None:
            try:
                from app.dependencies import get_qdrant
                from app.services.learning_vector_index import set_artifact_payload

                qdrant = get_qdrant()
                await set_artifact_payload(
                    qdrant._client,
                    artifact_id=artifact_id,
                    payload={
                        "artifact_scope": "candidate",
                        "status": "archived",
                        "updated_at": now,
                        "rejected_by": (rejected_by or "user").strip() or "user",
                        "rejection_source": (
                            (rejection_source or "inline_user_approval").strip() or "inline_user_approval"
                        ),
                        "rejected_at": now,
                    },
                )
            except Exception as exc:
                logger.debug("Learning vector index payload update skipped (non-fatal): %s", exc)
        return updated

    async def defer_candidate(
        self,
        artifact_id: UUID,
        defer_days: Optional[int] = None,
        *,
        deferred_by: str = "user",
        defer_source: str = "inline_user_approval",
        defer_reason: str = "",
    ) -> Optional[dict]:
        """
        Defer a candidate:
        - raises evidence_count threshold by +3 (min_evidence must accumulate more)
        - sets next_surface_after = now + effective_days
        - doubles wait on each subsequent defer (base 7d → 14 → 28 → ... capped at 90d)
        - auto-archives when cap reached
        """
        now = time.time()

        def _do() -> Optional[dict]:
            with self._lock:
                row = self._conn.execute(
                    "SELECT artifact_scope, status, defer_count, meta_json FROM artifacts WHERE id = ?",
                    (str(artifact_id),),
                ).fetchone()
                if (
                    row is None
                    or row["artifact_scope"] != "candidate"
                    or row["status"] != "pending_review"
                ):
                    return None

                defer_count = row["defer_count"] + 1
                base = defer_days if defer_days is not None else _DEFER_BASE_DAYS
                effective_days = min(base * (2 ** (defer_count - 1)), _DEFER_MAX_DAYS)
                next_surface = now + effective_days * 86400
                status = "archived" if effective_days >= _DEFER_MAX_DAYS else "pending_review"
                meta = json.loads(row["meta_json"] or "{}")
                meta["last_deferred_by"] = (deferred_by or "user").strip() or "user"
                meta["last_defer_source"] = (
                    (defer_source or "inline_user_approval").strip() or "inline_user_approval"
                )
                meta["last_deferred_at"] = now
                if defer_reason.strip():
                    meta["last_defer_reason"] = defer_reason.strip()

                self._conn.execute(
                    """UPDATE artifacts
                       SET defer_count = ?, next_surface_after = ?, status = ?,
                           updated_at = ?, meta_json = ?
                       WHERE id = ?""",
                    (defer_count, next_surface, status, now, json.dumps(meta), str(artifact_id)),
                )
                self._conn.commit()
                updated = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
                ).fetchone()
            return _row_to_dict(updated) if updated else None

        updated = await self._run_sync(_do)
        if updated is not None:
            try:
                from app.dependencies import get_qdrant
                from app.services.learning_vector_index import set_artifact_payload

                qdrant = get_qdrant()
                await set_artifact_payload(
                    qdrant._client,
                    artifact_id=artifact_id,
                    payload={
                        "artifact_scope": "candidate",
                        "status": str(updated.get("status") or "pending_review"),
                        "updated_at": now,
                        "last_deferred_by": (deferred_by or "user").strip() or "user",
                        "last_defer_source": (
                            (defer_source or "inline_user_approval").strip() or "inline_user_approval"
                        ),
                        "last_deferred_at": now,
                    },
                )
            except Exception as exc:
                logger.debug("Learning vector index payload update skipped (non-fatal): %s", exc)
        return updated

    # ── Report ────────────────────────────────────────────────────────────────

    async def get_report_candidates(self, limit: int = 3) -> list[dict]:
        """
        Top candidates for human review.
        Filters:
          - scope = candidate, status = pending_review
          - next_surface_after <= now
          - evidence_count >= min_evidence for the artifact's action_type
        Ranked by confidence * evidence_count DESC.
        """
        now = time.time()

        def _fetch() -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE artifact_scope = 'candidate'
                      AND status = 'pending_review'
                      AND (next_surface_after IS NULL OR next_surface_after <= ?)
                    ORDER BY (confidence * evidence_count) DESC
                    LIMIT ?
                    """,
                    (now, limit * 4),  # over-fetch, then filter by evidence threshold
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

        rows = await self._run_sync(_fetch)

        from app.config import settings
        skip_threshold = settings.glm_skip_evidence_threshold
        candidates = []
        for d in rows:
            defer_count = int(d.get("defer_count") or 0)
            threshold = min_evidence_for(d.get("action_type") or "") + defer_count * 3
            if skip_threshold or (d.get("evidence_count") or 0) >= threshold:
                candidates.append(d)
            if len(candidates) >= limit:
                break
        return candidates

    # ── Ledger mirror ─────────────────────────────────────────────────────────

    async def ledger_mirror(
        self,
        context_signature: str,
        limit: int = 5,
    ) -> list[dict]:
        """
        Return top promoted_pattern artifacts matching (or close to) the given context_signature.
        Used as a deterministic suggestion source in get_adaptation_suggestions.
        """

        def _do() -> list[dict]:
            with self._lock:
                # Exact match first
                rows = self._conn.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE artifact_scope = 'promoted_pattern'
                      AND status = 'active'
                      AND context_signature = ?
                    ORDER BY confidence DESC
                    LIMIT ?
                    """,
                    (context_signature, limit),
                ).fetchall()
                result = [_row_to_dict(r) for r in rows]

                # If not enough, fall back to partial match on project field
                if len(result) < limit:
                    project_part = next(
                        (seg for seg in context_signature.split(";") if seg.startswith("project=")),
                        None,
                    )
                    if project_part:
                        seen = {r["id"] for r in result}
                        rows2 = self._conn.execute(
                            """
                            SELECT * FROM artifacts
                            WHERE artifact_scope = 'promoted_pattern'
                              AND status = 'active'
                              AND context_signature LIKE ?
                              AND id NOT IN ({})
                            ORDER BY confidence DESC
                            LIMIT ?
                            """.format(",".join("?" * len(seen)) if seen else "SELECT NULL"),
                            ([f"%{project_part}%"] + list(seen) + [limit - len(result)])
                            if seen
                            else [f"%{project_part}%", limit - len(result)],
                        ).fetchall()
                        result.extend(_row_to_dict(r) for r in rows2)
            return result

        return await self._run_sync(_do)

    # ── Artifacts — read ──────────────────────────────────────────────────────

    async def get_artifact(self, artifact_id: UUID) -> Optional[dict]:
        def _do() -> Optional[dict]:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
                ).fetchone()
            return _row_to_dict(row) if row else None

        return await self._run_sync(_do)

    async def count_active_artifacts(
        self,
        project: str = "",
        context_signature: str = "",
    ) -> int:
        """Count active (non-candidate, non-archived) artifacts for a project/context."""
        clauses = ["artifact_scope != 'candidate'", "status = 'active'"]
        params: list = []
        if project:
            clauses.append("(context_signature LIKE ? OR context_signature LIKE ?)")
            params.append(f"%project={project}%")
            params.append(f"%project={project};%")
        if context_signature:
            clauses.append("context_signature = ?")
            params.append(context_signature)
        where = "WHERE " + " AND ".join(clauses)

        def _do() -> int:
            with self._lock:
                row = self._conn.execute(
                    f"SELECT COUNT(*) FROM artifacts {where}", params
                ).fetchone()
            return row[0] if row else 0

        return await self._run_sync(_do)

    async def get_pending_scout_hints(self, project: str = "", limit: int = 3) -> list[dict]:
        """Return pending external best-practice scout candidates awaiting user review."""
        params: list = []
        clauses = [
            "tags LIKE '%external%'",
            "tags LIKE '%best-practice%'",
            "status = 'pending_review'",
        ]
        if project:
            clauses.append("(context_signature LIKE ? OR context_signature LIKE ?)")
            params.append(f"%project={project}%")
            params.append(f"%project={project};%")
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)

        def _do() -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT id, tags, meta_json, content, observation FROM artifacts "
                    f"{where} ORDER BY created_at DESC LIMIT ?",
                    params,
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return await self._run_sync(_do)

    async def list_artifacts(
        self,
        agent_id: Optional[str] = None,
        artifact_type: Optional[str] = None,
        scope: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if artifact_type:
            clauses.append("artifact_type = ?")
            params.append(artifact_type)
        if scope:
            clauses.append("artifact_scope = ?")
            params.append(scope)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        def _do() -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM artifacts {where} ORDER BY created_at DESC LIMIT ?",
                    params,
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return await self._run_sync(_do)

    # ── Decay ─────────────────────────────────────────────────────────────────

    async def set_artifact_status(
        self,
        artifact_id: UUID,
        *,
        status: str,
        acted_by: str = "system",
        action_source: str = "inline_user_approval",
        reason: str = "",
    ) -> Optional[dict]:
        now = time.time()

        def _do() -> Optional[dict]:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?",
                    (str(artifact_id),),
                ).fetchone()
                if row is None:
                    return None
                meta = json.loads(row["meta_json"] or "{}")
                meta["status_updated_by"] = (acted_by or "system").strip() or "system"
                meta["status_update_source"] = (action_source or "inline_user_approval").strip() or "inline_user_approval"
                meta["status_updated_at"] = now
                meta["last_review_action"] = f"set_status:{status}"
                if reason.strip():
                    meta["status_update_reason"] = reason.strip()
                self._conn.execute(
                    "UPDATE artifacts SET status = ?, updated_at = ?, meta_json = ? WHERE id = ?",
                    (status, now, json.dumps(meta), str(artifact_id)),
                )
                self._conn.commit()
                updated = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?",
                    (str(artifact_id),),
                ).fetchone()
            return _row_to_dict(updated) if updated else None

        updated = await self._run_sync(_do)
        if updated is not None:
            try:
                from app.dependencies import get_qdrant
                from app.services.learning_vector_index import set_artifact_payload

                qdrant = get_qdrant()
                await set_artifact_payload(
                    qdrant._client,
                    artifact_id=artifact_id,
                    payload={"status": status, "updated_at": now},
                )
            except Exception as exc:
                logger.debug("Learning vector index payload status update skipped (non-fatal): %s", exc)
        return updated

    async def decay_stale_artifacts(
        self,
        inactivity_days: int = 30,
        reject_threshold: int = 5,
    ) -> int:
        """
        Archive artifacts that are stale or consistently rejected.
        Returns count of archived rows.
        """
        now = time.time()
        cutoff = now - inactivity_days * 86400

        def _do() -> int:
            with self._lock:
                cur = self._conn.execute(
                    """
                    UPDATE artifacts SET status = 'archived', updated_at = ?
                    WHERE status = 'active'
                      AND (
                        updated_at < ?
                        OR not_useful_votes >= ?
                      )
                    """,
                    (now, cutoff, reject_threshold),
                )
                self._conn.commit()
                return int(cur.rowcount)

        return await self._run_sync(_do)

    def close(self) -> None:
        if self._writer_stop is not None:
            try:
                self._writer_stop.set()
            except Exception:
                pass
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
        try:
            self._executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass


# ── Singleton ──────────────────────────────────────────────────────────────────

_store: Optional[LearningStore] = None


def get_learning_store() -> LearningStore:
    global _store
    if _store is None:
        _store = LearningStore()
    return _store


async def close_learning_store() -> None:
    global _store
    if _store is not None:
        await _store.aclose()
        _store = None
