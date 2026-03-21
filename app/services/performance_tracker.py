"""
Performance Tracker — SQLite log of every task execution.

Records: component, task_type, success, latency_ms, agent_id, metadata, corrected_task_type
Aggregates: success rates, avg latency, trends over time, task_type correction signals
Auto-syncs to Capability Registry when enough new events accumulate.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

# After this many new events for a component+task, sync to capability registry
REGISTRY_SYNC_THRESHOLD = 5

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS task_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   REAL    NOT NULL,
    component            TEXT    NOT NULL,
    task_type            TEXT    NOT NULL,
    success              INTEGER NOT NULL,
    latency_ms           REAL,
    agent_id             TEXT,
    session_id           TEXT,
    metadata             TEXT,
    corrected_task_type  TEXT
);
CREATE INDEX IF NOT EXISTS idx_comp_task   ON task_events(component, task_type);
CREATE INDEX IF NOT EXISTS idx_ts          ON task_events(ts);
CREATE INDEX IF NOT EXISTS idx_task_type   ON task_events(task_type);
"""

class PerformanceTracker:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._pending: dict[tuple[str, str], int] = {}
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            # Migrate existing databases that lack corrected_task_type column
            try:
                self._conn.execute("ALTER TABLE task_events ADD COLUMN corrected_task_type TEXT")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
            # Add correction index after migration (safe for both new and existing DBs)
            try:
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_correction "
                    "ON task_events(task_type, corrected_task_type)"
                )
                self._conn.commit()
            except Exception:
                pass
        logger.info("Performance tracker initialized: %s", db_path)

    def record(
        self,
        component: str,
        task_type: str,
        success: bool,
        latency_ms: Optional[float] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        corrected_task_type: Optional[str] = None,
    ) -> int:
        """Record a task event. Returns event id.

        corrected_task_type: set when the specialist (cloud LLM) signals that the
        classified task_type was wrong — the actual task belongs to a different type.
        This is "Ivanov's feedback": 'this isn't my task, send it to Sidorov next time'.
        """
        with self._lock:
            row_id = self._conn.execute(
                "INSERT INTO task_events "
                "(ts, component, task_type, success, latency_ms, agent_id, session_id, metadata, corrected_task_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    component,
                    task_type,
                    1 if success else 0,
                    latency_ms,
                    agent_id,
                    session_id,
                    json.dumps(metadata) if metadata else None,
                    corrected_task_type,
                ),
            ).lastrowid
            self._conn.commit()

        # Track pending for registry sync
        key = (component, task_type)
        self._pending[key] = self._pending.get(key, 0) + 1
        if self._pending[key] >= REGISTRY_SYNC_THRESHOLD:
            self._pending[key] = 0
            self._sync_to_registry(component, task_type)

        return row_id

    def _sync_to_registry(self, component: str, task_type: str) -> None:
        """Sync aggregate stats to capability registry."""
        try:
            from app.services.capability_registry import get_registry
            with self._lock:
                row = self._conn.execute(
                    "SELECT SUM(success), COUNT(*) - SUM(success) FROM task_events "
                    "WHERE component=? AND task_type=?",
                    (component, task_type),
                ).fetchone()
            if row and row[0] is not None:
                success_count = int(row[0])
                fail_count = int(row[1])
                reg = get_registry()
                entry = reg._data.setdefault(component, {}).setdefault(task_type, {"success": 0, "fail": 0})
                # Replace seed counts with real data once we have enough
                total = success_count + fail_count
                if total >= 10:
                    entry["success"] = success_count
                    entry["fail"] = fail_count
                    reg._save()
                    logger.info("Registry synced: %s/%s success=%d fail=%d", component, task_type, success_count, fail_count)
        except Exception as e:
            logger.warning("Registry sync failed: %s", e)

    def stats(
        self,
        component: Optional[str] = None,
        task_type: Optional[str] = None,
        since_hours: Optional[float] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        """Aggregate stats per component+task_type with optional scope filters."""
        where = []
        params: list = []
        if component:
            where.append("component = ?")
            params.append(component)
        if task_type:
            where.append("task_type = ?")
            params.append(task_type)
        if since_hours:
            where.append("ts >= ?")
            params.append(time.time() - since_hours * 3600)
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT component, task_type, "
                f"COUNT(*) as total, SUM(success) as ok, "
                f"AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms END) as avg_ms, "
                f"MIN(ts) as first_ts, MAX(ts) as last_ts "
                f"FROM task_events {clause} "
                f"GROUP BY component, task_type "
                f"ORDER BY component, task_type",
                params,
            ).fetchall()

        result = []
        for component, task_type, total, ok, avg_ms, first_ts, last_ts in rows:
            ok = ok or 0
            fail = total - ok
            rate = ok / total if total else 0.0
            result.append({
                "component": component,
                "task_type": task_type,
                "total": total,
                "success": ok,
                "fail": fail,
                "success_rate": round(rate, 3),
                "avg_latency_ms": round(avg_ms, 1) if avg_ms else None,
                "first_seen": first_ts,
                "last_seen": last_ts,
            })
        return result

    def percentiles(
        self,
        component: Optional[str] = None,
        task_type: Optional[str] = None,
        since_hours: Optional[float] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, dict]:
        """Latency percentiles (p50/p95/p99) per task_type."""
        where = ["latency_ms IS NOT NULL"]
        params: list = []
        if component:
            where.append("component = ?")
            params.append(component)
        if task_type:
            where.append("task_type = ?")
            params.append(task_type)
        if since_hours:
            where.append("ts >= ?")
            params.append(time.time() - since_hours * 3600)
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)

        clause = f"WHERE {' AND '.join(where)}"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT task_type, latency_ms FROM task_events {clause} ORDER BY task_type, latency_ms",
                params,
            ).fetchall()

        # Group latencies by task_type
        grouped: dict[str, list[float]] = {}
        for tt, ms in rows:
            grouped.setdefault(tt, []).append(ms)

        result = {}
        for tt, values in grouped.items():
            n = len(values)

            def pct(p: float) -> float:
                idx = max(0, int(p / 100 * n) - 1)
                return round(values[idx], 1)

            result[tt] = {"p50": pct(50), "p95": pct(95), "p99": pct(99), "n": n}
        return result

    def history(
        self,
        limit: int = 50,
        component: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> list[dict]:
        """Recent task events."""
        where = []
        params: list = []
        if component:
            where.append("component = ?")
            params.append(component)
        if task_type:
            where.append("task_type = ?")
            params.append(task_type)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, ts, component, task_type, success, latency_ms, agent_id, metadata "
                f"FROM task_events {clause} ORDER BY ts DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [
            {
                "id": r[0], "ts": r[1], "component": r[2], "task_type": r[3],
                "success": bool(r[4]), "latency_ms": r[5], "agent_id": r[6],
                "metadata": json.loads(r[7]) if r[7] else None,
            }
            for r in rows
        ]

    def corrections(
        self,
        task_type: Optional[str] = None,
        min_count: int = 1,
        since_hours: Optional[float] = None,
    ) -> list[dict]:
        """Aggregate task_type correction signals from specialist feedback.

        Returns rows of (classified_as, actual_type, count, correction_rate) —
        the dispatcher's learning table: 'when dyadya Petya says X, Ivanov says it's actually Y'.
        Only includes rows where corrected_task_type IS NOT NULL and differs from task_type.
        """
        where = ["corrected_task_type IS NOT NULL", "corrected_task_type != task_type"]
        params: list = []
        if task_type:
            where.append("task_type = ?")
            params.append(task_type)
        if since_hours:
            where.append("ts >= ?")
            params.append(time.time() - since_hours * 3600)
        clause = f"WHERE {' AND '.join(where)}"

        with self._lock:
            rows = self._conn.execute(
                f"SELECT task_type, corrected_task_type, COUNT(*) as cnt "
                f"FROM task_events {clause} "
                f"GROUP BY task_type, corrected_task_type "
                f"HAVING cnt >= ? "
                f"ORDER BY cnt DESC",
                params + [min_count],
            ).fetchall()

            # Also get total events per task_type to compute correction_rate
            totals_rows = self._conn.execute(
                "SELECT task_type, COUNT(*) FROM task_events GROUP BY task_type"
            ).fetchall()

        totals = {r[0]: r[1] for r in totals_rows}
        result = []
        for classified_as, actual_type, cnt in rows:
            total = totals.get(classified_as, cnt)
            result.append({
                "classified_as": classified_as,
                "actual_type": actual_type,
                "count": cnt,
                "correction_rate": round(cnt / total, 3) if total else 0.0,
            })
        return result

    def trends(self, component: str, task_type: str, buckets: int = 10) -> list[dict]:
        """Rolling success rate over time (bucketed)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, success FROM task_events WHERE component=? AND task_type=? ORDER BY ts",
                (component, task_type),
            ).fetchall()
        if not rows:
            return []
        min_ts, max_ts = rows[0][0], rows[-1][0]
        if max_ts == min_ts:
            return []
        bucket_size = (max_ts - min_ts) / buckets
        result = []
        for b in range(buckets):
            lo = min_ts + b * bucket_size
            hi = lo + bucket_size
            bucket_rows = [r for r in rows if lo <= r[0] < hi]
            if not bucket_rows:
                continue
            ok = sum(r[1] for r in bucket_rows)
            result.append({
                "ts": round(lo),
                "total": len(bucket_rows),
                "success_rate": round(ok / len(bucket_rows), 3),
            })
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Singleton
_tracker: Optional[PerformanceTracker] = None


def get_tracker() -> PerformanceTracker:
    global _tracker
    if _tracker is None:
        _tracker = PerformanceTracker(Path("qdrant_data") / "performance.db")
    return _tracker
