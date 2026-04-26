"""
Model Registry — quota-aware multi-model routing.

Tracks daily token/request usage per cloud model and ranks models by a
composite score: wilson_score(model_id, task_type) × remaining_fraction.

Storage:
  qdrant_data/model_registry.json — static model config + initial scores
  qdrant_data/quota.db            — SQLite: quota_usage, limit_events, handoff_log
"""

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional

from app.services.capability_registry import get_registry
from app.services.cloud_llm import configured_cloud_model_profiles

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS quota_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    TEXT    NOT NULL,
    date_utc    TEXT    NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    limit_val   INTEGER NOT NULL,
    limit_unit  TEXT    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_date ON quota_usage(model_id, date_utc);

CREATE TABLE IF NOT EXISTS limit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    model_id    TEXT    NOT NULL,
    error_code  TEXT,
    error_msg   TEXT,
    retry_after INTEGER
);

CREATE TABLE IF NOT EXISTS handoff_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    task_id     TEXT    NOT NULL,
    handoff_label TEXT,
    from_agent  TEXT    NOT NULL,
    to_agent    TEXT    NOT NULL,
    memory_id   TEXT    NOT NULL,
    reason      TEXT
);
"""

_DEFAULT_DAILY_LIMIT = 100_000
_DEFAULT_LIMIT_UNIT = "tokens"
_DEFAULT_TASK_CAPABILITIES = [
    "code_generation",
    "code_review",
    "architecture",
    "text_summarization",
    "fact_extraction",
]


@dataclass
class ModelQuota:
    model_id: str
    display_name: str
    provider: str
    daily_limit: int
    limit_unit: str
    used_today: int
    remaining: int
    remaining_fraction: float
    priority: int
    task_capabilities: list
    cooldown_until: Optional[float]
    is_available: bool
    weekly_limit: Optional[int] = None


def _wilson_score(success: int, fail: int, z: float = 1.28) -> float:
    n = success + fail
    if n == 0:
        return 0.0
    p = success / n
    return (p + z*z/(2*n) - z * math.sqrt((p*(1-p) + z*z/(4*n))/n)) / (1 + z*z/n)


class ModelRegistry:
    def __init__(self, config_path: Path, db_path: Path):
        self._config_path = config_path
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(_CREATE_SQL)
            self._ensure_schema()
            self._conn.commit()
        self._models: dict[str, dict] = {}
        self._load_config()
        logger.info("Model registry initialized: %d models", len(self._models))

    def _ensure_schema(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(handoff_log)").fetchall()}
        if "handoff_label" not in columns:
            self._conn.execute("ALTER TABLE handoff_log ADD COLUMN handoff_label TEXT")

    def _load_config(self) -> None:
        loaded_models: dict[str, dict] = {}
        if self._config_path.exists():
            try:
                payload = json.loads(self._config_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    loaded_models = payload
            except Exception as e:
                logger.warning("Failed to load model_registry.json: %s", e)

        manual_models = {
            model_id: model
            for model_id, model in loaded_models.items()
            if isinstance(model, dict) and str(model.get("managed_by") or "").strip().lower() == "manual"
        }
        configured_models = self._configured_models(existing_models=loaded_models)
        self._models = {**manual_models, **configured_models}

        if self._models != loaded_models:
            self._save_config()

        self._seed_capability_registry()

    def _configured_models(self, *, existing_models: dict[str, dict] | None = None) -> dict[str, dict]:
        existing_models = existing_models or {}
        profiles = configured_cloud_model_profiles()
        models: dict[str, dict] = {}

        for priority, (model_id, profile) in enumerate(profiles.items(), start=1):
            existing = existing_models.get(model_id, {}) if isinstance(existing_models.get(model_id), dict) else {}
            daily_limit = int(existing.get("daily_limit") or _DEFAULT_DAILY_LIMIT)
            limit_unit = str(existing.get("limit_unit") or _DEFAULT_LIMIT_UNIT).strip() or _DEFAULT_LIMIT_UNIT
            weekly_limit = existing.get("weekly_limit")
            if weekly_limit is None:
                weekly_limit = daily_limit * 7
            task_capabilities = existing.get("task_capabilities")
            if not isinstance(task_capabilities, list) or not task_capabilities:
                task_capabilities = list(_DEFAULT_TASK_CAPABILITIES)
            initial_scores = existing.get("initial_scores")
            if not isinstance(initial_scores, dict):
                initial_scores = {}

            models[model_id] = {
                "model_id": model_id,
                "display_name": str(existing.get("display_name") or profile.model).strip() or model_id,
                "provider": str(profile.provider or existing.get("provider") or "openai-compatible").strip(),
                "daily_limit": daily_limit,
                "limit_unit": limit_unit,
                "priority": int(existing.get("priority") or priority),
                "weekly_limit": weekly_limit,
                "task_capabilities": task_capabilities,
                "initial_scores": initial_scores,
                "managed_by": "config",
            }

        return models

    def _save_config(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps(self._models, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.error("Failed to save model_registry.json: %s", e)

    def _seed_capability_registry(self) -> None:
        try:
            reg = get_registry()
            for m in self._models.values():
                for task_type, score in m.get("initial_scores", {}).items():
                    reg.register(m["model_id"], task_type, score, f"{m['display_name']} — cloud")
        except Exception as e:
            logger.warning("Capability registry seed failed: %s", e)

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_quota_row(self, model_id: str) -> tuple[int, int, str]:
        """Return (used, limit_val, limit_unit) for today, inserting row if missing."""
        model = self._models.get(model_id)
        if not model:
            return 0, 0, "tokens"
        daily_limit = model.get("daily_limit", 0)
        limit_unit = model.get("limit_unit", "tokens")
        today = self._today()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO quota_usage (model_id, date_utc, used, limit_val, limit_unit, updated_at) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (model_id, today, daily_limit, limit_unit, time.time()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT used, limit_val, limit_unit FROM quota_usage WHERE model_id=? AND date_utc=?",
                (model_id, today),
            ).fetchone()
        return (row[0], row[1], row[2]) if row else (0, daily_limit, limit_unit)

    def _window_bounds(self, days: int, now: datetime) -> tuple[datetime, datetime]:
        """Return (start, end) datetimes for the last `days` window ending today."""
        start = now - timedelta(days=days - 1)
        start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end = start + timedelta(days=days)
        return start, end

    def _sum_usage_last_days(self, model_id: str, days: int) -> int:
        """Sum consumed units for the past `days` calendar days (inclusive)."""
        if days <= 1:
            used, _, _ = self._ensure_quota_row(model_id)
            return used
        self._ensure_quota_row(model_id)
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(used) FROM quota_usage WHERE model_id=? AND date_utc>=?",
                (model_id, cutoff_date),
            ).fetchone()
        return int(row[0] or 0)

    def _limit_for_window(self, model: dict, days: int, limit_key: str) -> int:
        """Return the configured limit for window or fallback to scaled daily limit."""
        limit = model.get(limit_key)
        if limit is None:
            base = model.get("daily_limit", 0)
            limit = base * days if days > 1 else base
        return limit or 0

    def _get_cooldown(self, model_id: str) -> Optional[float]:
        """Return cooldown_until timestamp if model is in cooldown, else None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, retry_after FROM limit_events WHERE model_id=? ORDER BY ts DESC LIMIT 1",
                (model_id,),
            ).fetchone()
        if not row:
            return None
        ts, retry_after = row
        if retry_after:
            cooldown_until = ts + retry_after
            if time.time() < cooldown_until:
                return cooldown_until
        return None

    def _build_quota(self, model_id: str) -> ModelQuota:
        model = self._models[model_id]
        used, limit_val, limit_unit = self._ensure_quota_row(model_id)
        remaining = max(0, limit_val - used)
        remaining_fraction = remaining / limit_val if limit_val > 0 else 0.0
        cooldown_until = self._get_cooldown(model_id)
        is_available = cooldown_until is None and remaining_fraction > 0.0
        return ModelQuota(
            model_id=model_id,
            display_name=model.get("display_name", model_id),
            provider=model.get("provider", "unknown"),
            daily_limit=limit_val,
            limit_unit=limit_unit,
            used_today=used,
            remaining=remaining,
            remaining_fraction=round(remaining_fraction, 4),
            priority=model.get("priority", 99),
            task_capabilities=model.get("task_capabilities", []),
            cooldown_until=cooldown_until,
            is_available=is_available,
            weekly_limit=model.get("weekly_limit"),
        )

    def register(
        self,
        model_id: str,
        display_name: str,
        provider: str,
        daily_limit: int,
        limit_unit: str = "tokens",
        priority: int = 99,
        task_capabilities: Optional[list] = None,
        initial_scores: Optional[dict] = None,
        weekly_limit: Optional[int] = None,
        managed_by: str = "manual",
    ) -> ModelQuota:
        """Register or update a model config."""
        self._models[model_id] = {
            "model_id": model_id,
            "display_name": display_name,
            "provider": provider,
            "daily_limit": daily_limit,
            "limit_unit": limit_unit,
            "priority": priority,
            "task_capabilities": task_capabilities or [],
            "initial_scores": initial_scores or {},
            "weekly_limit": weekly_limit,
            "managed_by": managed_by,
        }
        self._save_config()
        # Seed capability registry for new scores
        if initial_scores:
            try:
                reg = get_registry()
                for task_type, score in initial_scores.items():
                    reg.register(model_id, task_type, score, f"{display_name} — cloud")
            except Exception as e:
                logger.warning("Capability seed failed for %s: %s", model_id, e)
        return self._build_quota(model_id)

    def rank_for_task(self, task_type: str) -> list[tuple[str, float]]:
        """
        Return available models ranked by composite score:
          wilson_score(model_id, task_type) × remaining_fraction
        Excludes models in cooldown or at quota.
        Falls back to all models with remaining quota if none have wilson scores.
        """
        results = []
        try:
            reg = get_registry()
        except Exception:
            reg = None

        fallback_score = 0.0
        if reg:
            fallback_score = reg.score("cloud-llm", task_type) * 0.9

        for model_id, model in self._models.items():
            # Only include models capable of this task
            caps = model.get("task_capabilities", [])
            if task_type not in caps and caps:
                continue

            quota = self._build_quota(model_id)
            if not quota.is_available:
                continue

            # Wilson score from capability registry
            wilson = reg.score(model_id, task_type) if reg else 0.0
            if wilson == 0.0:
                wilson = fallback_score if fallback_score > 0 else 0.5

            composite = wilson * quota.remaining_fraction
            # Boost by priority (higher priority = smaller number = slight boost)
            priority_boost = 1.0 / (1.0 + model.get("priority", 99) * 0.05)
            composite *= priority_boost

            results.append((model_id, round(composite, 4)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def record_usage(self, model_id: str, units_used: int) -> ModelQuota:
        """Add units_used to today's quota."""
        if model_id not in self._models:
            raise ValueError(f"Unknown model: {model_id}")
        self._ensure_quota_row(model_id)
        today = self._today()
        with self._lock:
            self._conn.execute(
                "UPDATE quota_usage SET used = used + ?, updated_at = ? WHERE model_id=? AND date_utc=?",
                (units_used, time.time(), model_id, today),
            )
            self._conn.commit()
        return self._build_quota(model_id)

    def report_limit_hit(
        self,
        model_id: str,
        error_code: Optional[str] = None,
        error_msg: Optional[str] = None,
        retry_after: Optional[int] = None,
    ) -> ModelQuota:
        """Record that a model hit its rate/quota limit."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO limit_events (ts, model_id, error_code, error_msg, retry_after) VALUES (?, ?, ?, ?, ?)",
                (time.time(), model_id, error_code, error_msg, retry_after or 3600),
            )
            self._conn.commit()
        logger.info("Limit hit recorded for %s (retry_after=%s)", model_id, retry_after)
        return self._build_quota(model_id)

    def available(self, task_type: Optional[str] = None) -> list[ModelQuota]:
        """Return all available models, optionally filtered by task_type capability."""
        result = []
        for model_id in self._models:
            quota = self._build_quota(model_id)
            if task_type and task_type not in self._models[model_id].get("task_capabilities", []):
                continue
            result.append(quota)
        result.sort(key=lambda q: q.priority)
        return result

    def get_model(self, model_id: str) -> ModelQuota:
        if model_id not in self._models:
            raise KeyError(f"Model not found: {model_id}")
        return self._build_quota(model_id)

    def log_handoff(
        self,
        task_id: str,
        handoff_label: Optional[str],
        from_agent: str,
        to_agent: str,
        memory_id: str,
        reason: Optional[str] = None,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO handoff_log (ts, task_id, handoff_label, from_agent, to_agent, memory_id, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), task_id, handoff_label, from_agent, to_agent, memory_id, reason),
            )
            self._conn.commit()
        return cursor.lastrowid

    def handoff_log(self, limit: int = 20, handoff_label: Optional[str] = None) -> list[dict]:
        with self._lock:
            if handoff_label:
                rows = self._conn.execute(
                    "SELECT id, ts, task_id, handoff_label, from_agent, to_agent, memory_id, reason FROM handoff_log WHERE handoff_label = ? ORDER BY ts DESC LIMIT ?",
                    (handoff_label, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, ts, task_id, handoff_label, from_agent, to_agent, memory_id, reason FROM handoff_log ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {"id": r[0], "ts": r[1], "task_id": r[2], "handoff_label": r[3], "from_agent": r[4],
             "to_agent": r[5], "memory_id": r[6], "reason": r[7]}
            for r in rows
        ]

    def reset_quota(self, model_id: str) -> ModelQuota:
        """Reset today's usage to 0 (admin/testing)."""
        today = self._today()
        with self._lock:
            self._conn.execute(
                "UPDATE quota_usage SET used=0, updated_at=? WHERE model_id=? AND date_utc=?",
                (time.time(), model_id, today),
            )
            # Clear cooldown events for this model
            self._conn.execute("DELETE FROM limit_events WHERE model_id=?", (model_id,))
            self._conn.commit()
        return self._build_quota(model_id)

    def status_dashboard(self) -> list[dict]:
        """Return quota status for all models."""
        now = datetime.now(timezone.utc)
        window_specs = [
            {"name": "daily", "days": 1, "limit_key": "daily_limit", "label": "Daily"},
            {"name": "weekly", "days": 7, "limit_key": "weekly_limit", "label": "Weekly"},
        ]
        result = []
        for model_id, model in self._models.items():
            quota = self._build_quota(model_id)
            cooldown_secs = None
            if quota.cooldown_until:
                cooldown_secs = max(0, int(quota.cooldown_until - time.time()))

            windows = []
            for spec in window_specs:
                start, end = self._window_bounds(spec["days"], now)
                limit = self._limit_for_window(model, spec["days"], spec["limit_key"])
                used = self._sum_usage_last_days(model_id, spec["days"])
                remaining = max(limit - used, 0)
                percent = round((used / limit) * 100, 1) if limit else 0.0
                percent = min(percent, 100.0)
                windows.append({
                    "name": spec["name"],
                    "label": spec["label"],
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": limit,
                    "used": used,
                    "remaining": remaining,
                    "percent": percent,
                    "next_reset": end.isoformat(),
                })

            result.append({
                "model_id": quota.model_id,
                "display_name": quota.display_name,
                "provider": quota.provider,
                "daily_limit": quota.daily_limit,
                "limit_unit": quota.limit_unit,
                "used_today": quota.used_today,
                "remaining": quota.remaining,
                "remaining_pct": round(quota.remaining_fraction * 100, 1),
                "priority": quota.priority,
                "task_capabilities": quota.task_capabilities,
                "is_available": quota.is_available,
                "cooldown_seconds": cooldown_secs,
                "snapshot_at": now.isoformat(),
                "next_reset": windows[0]["next_reset"],
                "windows": windows,
            })
        result.sort(key=lambda x: x["priority"])
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Singleton
_registry: Optional[ModelRegistry] = None


def get_model_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry(
            config_path=Path("qdrant_data") / "model_registry.json",
            db_path=Path("qdrant_data") / "quota.db",
        )
    return _registry
