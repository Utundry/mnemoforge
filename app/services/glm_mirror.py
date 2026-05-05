"""
Model Mirror — background job that analyzes dialogue-derived learning events
and generates candidate artifacts.

Design (human-in-the-loop):
  - Reads recent events from learning.db
  - Summarizes top behavioral patterns (by frequency)
  - Calls Ollama to generate structured candidate JSON (observation / why / rule)
  - Validates output (action_type whitelist + trigger DSL)
  - Calls upsert_candidate() — dedup-safe, never direct inserts
  - Records an llm_mirror event for auditability
  - Produces ONLY candidates (scope=candidate, status=pending_review)
  - Human approves/rejects/defers via GET /learning/report

Run modes:
  - Periodic: asyncio background loop (interval configurable via MODEL_MIRROR_INTERVAL_HOURS env)
  - Manual:   POST /learning/mirror/run

Legacy note:
  This module keeps the historical glm_mirror name as a compatibility shim.
  The canonical naming in new code is ModelMirror / get_model_mirror.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from app.services.llm_gateway import get_cloud_gateway

logger = logging.getLogger(__name__)


def _env_float(*names: str, default: str) -> float:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return float(value)
    return float(default)


def _env_str(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default

# How many candidates GLM proposes per run (matches UX principle: 2-3 max)
_MAX_CANDIDATES_PER_RUN = 3

# Analyse events from the last N hours
MODEL_MIRROR_LOOKBACK_HOURS = _env_float(
    "MODEL_MIRROR_LOOKBACK_HOURS",
    "GLM_MIRROR_LOOKBACK_HOURS",
    default="24",
)

# Minimum frequency for a pattern to be eligible (avoid noise)
_MIN_PATTERN_FREQ = 3

# Periodic interval (hours)
MODEL_MIRROR_INTERVAL_HOURS = _env_float(
    "MODEL_MIRROR_INTERVAL_HOURS",
    "GLM_MIRROR_INTERVAL_HOURS",
    default="0.1667",
)  # default 10min

# Generation model (must support text generation, NOT embedding-only models)
MODEL_MIRROR_GENERATE_MODEL = _env_str(
    "MODEL_MIRROR_MODEL",
    "GLM_GENERATE_MODEL",
    default="qwen3:1.7b",
)

# Language for candidate descriptions (observation / why_it_matters / proposed_content)
MODEL_MIRROR_RESPONSE_LANGUAGE = _env_str(
    "MODEL_MIRROR_RESPONSE_LANGUAGE",
    "GLM_RESPONSE_LANGUAGE",
    default="Russian",
)

# Allowed action_types and artifact_types (mirrors trigger_dsl whitelists)
from app.services.trigger_dsl import (
    ALLOWED_ACTION_TYPES,
    validate_if_then_rule,
    validate_trigger,
)
from app.services.data_hygiene_service import learning_event_should_be_excluded_from_learning
from app.services.text_localization import prepare_artifact_texts

_DIALOGUE_PRIMARY_EVENT_TYPES = frozenset({"dialogue_signal", "dialogue_excerpt"})
_DIALOGUE_SUPPORT_EVENT_TYPES = frozenset({"user_request", "user_feedback"})
_LEARNING_CONTEXT_EVENT_TYPES = _DIALOGUE_PRIMARY_EVENT_TYPES | _DIALOGUE_SUPPORT_EVENT_TYPES

_CANDIDATE_SCHEMA = {
    "action_type":      str,
    "artifact_type":    str,
    "trigger_dsl":      str,
    "observation":      str,
    "why_it_matters":   str,
    "proposed_content": str,
    "confidence":       float,
    "risk_level":       str,
    "evidence_count":   int,
}

_ALLOWED_ARTIFACT_TYPES = {"hint", "if_then_rule", "meta_guidance"}
_ALLOWED_RISK_LEVELS = {"low", "medium"}  # never high in GLM candidates


# ── Result ─────────────────────────────────────────────────────────────────────

@dataclass
class ModelMirrorResult:
    candidates_created: int = 0
    candidates_updated: int = 0   # evidence_count incremented on existing candidate
    events_analyzed: int = 0
    patterns_found: int = 0
    errors: list[str] = field(default_factory=list)     # fatal: candidate skipped
    warnings: list[str] = field(default_factory=list)   # non-fatal notices
    ran_at: float = field(default_factory=time.time)
    skipped_validation: int = 0

    def to_dict(self) -> dict:
        return {
            "candidates_created":  self.candidates_created,
            "candidates_updated":  self.candidates_updated,
            "events_analyzed":     self.events_analyzed,
            "patterns_found":      self.patterns_found,
            "errors":              self.errors,
            "warnings":            self.warnings,
            "ran_at":              self.ran_at,
            "skipped_validation":  self.skipped_validation,
        }


# ── Event summarizer ───────────────────────────────────────────────────────────

def _summarize_events(events: list[dict]) -> str:
    """
    Produce a compact text summary of recent events for the LLM prompt.
    Groups by (context_signature, event_type) with payload detail for
    user_request (request_type) and tool_call (tool_name).
    """
    if any(ev.get("event_type") in _LEARNING_CONTEXT_EVENT_TYPES for ev in events):
        return _summarize_dialogue_events(events)

    from collections import defaultdict

    # Group: (context_sig, event_type, detail) → count
    counts: dict[tuple, int] = defaultdict(int)
    for ev in events:
        ctx = ev.get("context_signature") or "unknown"
        etype = ev.get("event_type") or "unknown"
        detail = ""
        try:
            payload = json.loads(ev.get("payload_json") or "{}")
            if etype == "user_request":
                detail = payload.get("request_text") or payload.get("request_type") or ""
            elif etype == "user_feedback":
                detail = payload.get("feedback_text") or payload.get("valence") or ""
            elif etype == "tool_call":
                detail = payload.get("tool_name") or ""
        except Exception:
            pass
        counts[(ctx, etype, detail)] += 1

    # Sort by count desc, take top 15
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:15]

    lines = []
    for (ctx, etype, detail), cnt in top:
        desc = f"event:{etype}"
        if detail:
            desc += f"({detail})"
        lines.append(f"  count={cnt:3d} | context={ctx} | {desc}")

    return "\n".join(lines) if lines else "(no events)"


def _dialogue_signal_detail(payload: dict) -> str:
    parts: list[str] = []
    for key in ("missing_skill", "successful_pattern", "new_terminology", "user_preference", "domain_drift"):
        values = payload.get(key) or []
        if isinstance(values, list) and values:
            rendered = ",".join(str(v)[:40] for v in values[:4])
            parts.append(f"{key}={rendered}")
    excerpt = re.sub(r"\s+", " ", str(payload.get("excerpt") or "")).strip()
    if excerpt:
        parts.append(f"excerpt={excerpt[:180]}")
    return " | ".join(parts)


def _summarize_dialogue_events(events: list[dict]) -> str:
    lines: list[str] = []
    for ev in sorted(events, key=lambda x: float(x.get("ts") or 0), reverse=True)[:15]:
        etype = ev.get("event_type") or "unknown"
        try:
            payload = json.loads(ev.get("payload_json") or "{}")
        except Exception:
            payload = {}

        if etype == "dialogue_signal":
            detail = _dialogue_signal_detail(payload)
            if detail:
                lines.append(f"  - dialogue_signal | {detail}")
        elif etype == "dialogue_excerpt":
            excerpt = re.sub(r"\s+", " ", str(payload.get("excerpt") or "")).strip()
            if excerpt:
                lines.append(f"  - dialogue_excerpt | {excerpt[:220]}")
        elif etype == "user_request":
            detail = str(payload.get("request_text") or payload.get("request_type") or "").strip()
            if detail:
                lines.append(f"  - user_request | {detail[:180]}")
        elif etype == "user_feedback":
            detail = str(payload.get("feedback_text") or payload.get("valence") or "").strip()
            if detail:
                lines.append(f"  - user_feedback | {detail[:180]}")

    return "\n".join(lines) if lines else "(no dialogue-derived evidence)"


def _event_pattern_key(ev: dict) -> tuple[str, str]:
    etype = ev.get("event_type") or "unknown"
    ctx = ev.get("context_signature") or "unknown"
    try:
        payload = json.loads(ev.get("payload_json") or "{}")
    except Exception:
        payload = {}

    if etype == "dialogue_signal":
        detail = _dialogue_signal_detail({**payload, "excerpt": ""})
        return etype, detail or ctx
    if etype == "dialogue_excerpt":
        return etype, str(payload.get("source_path") or payload.get("file_hash") or ctx)
    if etype == "user_request":
        detail = str(payload.get("request_text") or payload.get("request_type") or "").strip()
        return etype, detail or ctx
    if etype == "user_feedback":
        detail = str(payload.get("feedback_text") or payload.get("valence") or "").strip()
        return etype, detail or ctx
    if etype == "tool_call":
        return etype, str(payload.get("tool_name") or ctx)
    return etype, ctx


def _select_events_for_analysis(events: list[dict]) -> tuple[list[dict], str]:
    dialogue_events = [e for e in events if e.get("event_type") in _DIALOGUE_PRIMARY_EVENT_TYPES]
    if dialogue_events:
        supporting = [e for e in events if e.get("event_type") in _DIALOGUE_SUPPORT_EVENT_TYPES]
        selected = sorted(dialogue_events + supporting, key=lambda x: float(x.get("ts") or 0))
        return selected[-120:], "dialogue"
    supporting = [e for e in events if e.get("event_type") in _DIALOGUE_SUPPORT_EVENT_TYPES]
    if supporting:
        return sorted(supporting, key=lambda x: float(x.get("ts") or 0))[-120:], "user_context"
    return [], "insufficient_context"


def _summarize_active_artifacts(artifacts: list[dict]) -> str:
    if not artifacts:
        return "(none)"
    lines = []
    for a in artifacts[:20]:
        action = a.get("action_type") or a.get("artifact_type") or "?"
        content = (a.get("content") or "")[:80]
        lines.append(f"  - {action}: {content}")
    return "\n".join(lines)


# ── LLM prompt ─────────────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
You are a behavioral pattern analyzer for an AI agent system.
Your job: examine recent event logs and suggest up to {max_candidates} automation rules
that would reduce repetitive work for the user.

PRIORITY:
- Prefer dialogue-derived evidence (dialogue excerpts, dialogue signals, explicit user feedback).
- Tool telemetry is fallback context only when dialogue evidence is absent.
- Do not learn mainly from repeated infrastructure/tool noise if dialogue evidence exists.

ALLOWED action_type values (use ONLY these):
{allowed_actions}

ALLOWED artifact_type values: hint, if_then_rule, meta_guidance

EVENT LOG (last {lookback_h:.0f} hours):
{event_summary}

ALREADY AUTOMATED (do NOT duplicate):
{active_summary}

OUTPUT RULES:
- Output ONLY a valid JSON array. No markdown, no explanation, no extra text.
- Each element must have ALL these keys (string unless noted):
    action_type, artifact_type, trigger_dsl, observation, why_it_matters,
    proposed_content, confidence (float), risk_level, evidence_count (int)
- observation, why_it_matters, proposed_content MUST be written in {language}
- observation: 1-2 sentences describing what you saw in the events
- why_it_matters: 1-2 sentences on why automating this saves the user time
- proposed_content: the actual hint or rule text the user will read
- confidence: float between 0.5 and 0.9 (never 1.0)
- risk_level: "low" or "medium" only
- trigger_dsl: leave "" for hints; for if_then_rule use the grammar:
    event(TYPE) or event(TYPE).field == "value" or event(TYPE).field in ["a","b"]
    Allowed TYPE: user_request, user_feedback, dialogue_excerpt, dialogue_signal, tool_call, tool_result, memory_write,
                  episode_end, artifact_suggested
- Only include patterns observed at least {min_freq} times
- If you find fewer than {max_candidates} strong patterns, output fewer items (even [])

OUTPUT:"""


# ── JSON parsing + validation ──────────────────────────────────────────────────

def _extract_json_array(text: str) -> list | None:
    """Extract first JSON array from LLM response (handles markdown fences)."""
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?", "", text).strip()
    # Find first [...] block
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def _normalize_candidate(raw: dict) -> dict:
    """Auto-correct common LLM mistakes before validation."""
    raw = dict(raw)
    # LLM often puts action_type value into artifact_type field
    # Always fix artifact_type if it's actually an action_type
    if raw.get("artifact_type", "") in ALLOWED_ACTION_TYPES:
        if not raw.get("action_type") or raw["action_type"] not in ALLOWED_ACTION_TYPES:
            raw["action_type"] = raw["artifact_type"]
        raw["artifact_type"] = "hint"
    return raw


def _validate_candidate(raw: dict) -> list[str]:
    """Return list of validation errors for a GLM-produced candidate dict."""
    errors: list[str] = []

    # Type checks
    for key, expected_type in _CANDIDATE_SCHEMA.items():
        val = raw.get(key)
        if val is None:
            errors.append(f"missing field: '{key}'")
        elif not isinstance(val, expected_type):
            # Allow int where float expected
            if expected_type is float and isinstance(val, int):
                pass
            else:
                errors.append(f"'{key}' must be {expected_type.__name__}, got {type(val).__name__}")

    if errors:
        return errors  # skip semantic checks if basic structure is wrong

    # Semantic checks
    action_type = raw["action_type"]
    artifact_type = raw["artifact_type"]
    trigger_dsl = raw.get("trigger_dsl") or ""
    risk_level = raw.get("risk_level") or "low"
    confidence = float(raw.get("confidence") or 0)

    if action_type not in ALLOWED_ACTION_TYPES:
        errors.append(f"action_type '{action_type}' not in whitelist")

    if artifact_type not in _ALLOWED_ARTIFACT_TYPES:
        errors.append(f"artifact_type '{artifact_type}' not in {_ALLOWED_ARTIFACT_TYPES}")

    if risk_level not in _ALLOWED_RISK_LEVELS:
        errors.append(f"risk_level '{risk_level}' not allowed (only low/medium)")

    if not (0.0 <= confidence <= 1.0):
        errors.append(f"confidence {confidence} out of range [0, 1]")

    if artifact_type == "if_then_rule" and trigger_dsl:
        dsl_errors = validate_if_then_rule(trigger_dsl, action_type)
        errors.extend(dsl_errors)
    elif artifact_type == "hint" and trigger_dsl:
        # hints may have optional trigger — validate if present
        dsl_errors = validate_trigger(trigger_dsl)
        errors.extend(dsl_errors)

    # Content sanity
    if not str(raw.get("observation") or "").strip():
        errors.append("observation must not be empty")
    if not str(raw.get("proposed_content") or "").strip():
        errors.append("proposed_content must not be empty")

    return errors


# ── Main service ───────────────────────────────────────────────────────────────

class ModelMirror:
    def __init__(self) -> None:
        self._last_result: Optional[ModelMirrorResult] = None
        self.next_run_at: Optional[float] = None

    async def run(self, ollama, learning_store) -> ModelMirrorResult:
        """
        Full GLM mirror cycle:
          1. Collect recent events
          2. Summarize patterns
          3. Call LLM
          4. Validate + risk-gate candidates
          5. upsert_candidate() (dedup-safe)
          6. Record llm_mirror event
        """
        result = ModelMirrorResult(ran_at=time.time())

        try:
            since_ts = time.time() - MODEL_MIRROR_LOOKBACK_HOURS * 3600
            events_all = await learning_store.list_events(since_ts=since_ts, limit=500)

            # Learn only from canonical, trigger-safe event types (see trigger_dsl whitelists).
            # This avoids self-referential loops dominated by llm_mirror / approvals / admin events.
            from app.services.trigger_dsl import ALLOWED_EVENT_TYPES
            events = [
                e for e in (events_all or [])
                if (e.get("event_type") in ALLOWED_EVENT_TYPES)
                and not learning_event_should_be_excluded_from_learning(e)
            ]
            events, analysis_source = _select_events_for_analysis(events)
            result.events_analyzed = len(events)

            if not events:
                if events_all:
                    result.warnings.append("insufficient_dialogue_evidence")
                    logger.info(
                        "Model mirror: skipped due to insufficient dialogue evidence in last %.0fh",
                        MODEL_MIRROR_LOOKBACK_HOURS,
                    )
                else:
                    logger.info(
                        "Model mirror: no events in last %.0fh — skipping",
                        MODEL_MIRROR_LOOKBACK_HOURS,
                    )
                self._last_result = result
                return result

            # Count patterns
            from collections import Counter
            pattern_counter: Counter = Counter()
            for ev in events:
                pattern_counter[_event_pattern_key(ev)] += 1

            eligible = [(k, v) for k, v in pattern_counter.items() if v >= _MIN_PATTERN_FREQ]
            result.patterns_found = len(eligible)

            if not eligible:
                logger.info("Model mirror: no patterns with freq >= %d — skipping", _MIN_PATTERN_FREQ)
                self._last_result = result
                return result

            # Active artifacts (to avoid duplicates in prompt)
            active_arts = await learning_store.list_artifacts(status="active", limit=50)

            # Build prompt
            event_summary = _summarize_events(events)
            active_summary = _summarize_active_artifacts(active_arts)
            prompt = _PROMPT_TEMPLATE.format(
                max_candidates=_MAX_CANDIDATES_PER_RUN,
                allowed_actions=", ".join(sorted(ALLOWED_ACTION_TYPES)),
                lookback_h=MODEL_MIRROR_LOOKBACK_HOURS,
                event_summary=event_summary,
                active_summary=active_summary,
                min_freq=_MIN_PATTERN_FREQ,
                language=MODEL_MIRROR_RESPONSE_LANGUAGE,
            )

            # Call LLM
            logger.info("Model mirror: calling LLM (source=%s, events=%d, patterns=%d)",
                        analysis_source, result.events_analyzed, result.patterns_found)
            raw_response = await get_cloud_gateway().generate(
                prompt,
                task_type="memory_extraction",
                mode="economy",
                max_tokens=1200,
                temperature=0.0,
                timeout=60.0,
                allow_local_fallback=True,
                prefer_local=True,
            )

            if not raw_response:
                result.errors.append("LLM returned empty response")
                self._last_result = result
                return result

            # Parse JSON
            candidates = _extract_json_array(raw_response)
            if candidates is None:
                result.errors.append(f"LLM response is not a valid JSON array: {raw_response[:200]}")
                self._last_result = result
                return result

            # Record llm_mirror event for auditability
            await learning_store.write_event(
                event_type="llm_mirror",
                agent_id="glm",
                payload={
                    "analysis_source": analysis_source,
                    "events_analyzed": result.events_analyzed,
                    "patterns_found":  result.patterns_found,
                    "candidates_raw":  len(candidates),
                },
            )

            # Validate + upsert each candidate
            for raw in candidates[:_MAX_CANDIDATES_PER_RUN]:
                if not isinstance(raw, dict):
                    result.errors.append(f"candidate is not a dict: {raw!r}")
                    result.skipped_validation += 1
                    continue

                raw = _normalize_candidate(raw)
                # GLM-generated DSL is consistently unreliable — always clear it.
                # Value is in observation / why_it_matters / proposed_content.
                raw = dict(raw)
                raw["trigger_dsl"] = ""
                errors = _validate_candidate(raw)

                if errors:
                    logger.warning("Model mirror: candidate rejected (%s): %s", errors, raw)
                    result.errors.extend(errors)
                    result.skipped_validation += 1
                    continue

                # Coerce types
                action_type      = str(raw["action_type"])
                artifact_type    = str(raw["artifact_type"])
                trigger_dsl      = str(raw.get("trigger_dsl") or "")
                observation      = str(raw.get("observation") or "")
                why_it_matters   = str(raw.get("why_it_matters") or "")
                proposed_content = str(raw.get("proposed_content") or "")
                confidence       = float(raw.get("confidence") or 0.7)
                risk_level       = str(raw.get("risk_level") or "low")
                cleaned_fields, enriched_meta = await prepare_artifact_texts(
                    content=proposed_content,
                    observation=observation,
                    why_it_matters=why_it_matters,
                    meta={"analysis_source": analysis_source},
                )

                artifact_id, created = await learning_store.upsert_candidate(
                    agent_id="glm",
                    action_type=action_type,
                    content=cleaned_fields["content"],
                    trigger_dsl=trigger_dsl,
                    observation=cleaned_fields["observation"],
                    why_it_matters=cleaned_fields["why_it_matters"],
                    risk_level=risk_level,
                    confidence=confidence,
                    artifact_type=artifact_type,
                    tags=["glm-mirror"],
                    meta=enriched_meta,
                )
                if created:
                    result.candidates_created += 1
                    logger.info("Model mirror: new candidate %s (action=%s)", artifact_id, action_type)
                else:
                    result.candidates_updated += 1
                    logger.info("Model mirror: evidence++ on %s (action=%s)", artifact_id, action_type)

                if confidence >= 0.8 and risk_level == "low":
                    result.warnings.append("user_review_required_for_high_confidence_candidates")

        except Exception as exc:
            logger.exception("Model mirror: unexpected error: %s", exc)
            result.errors.append(str(exc))

        self._last_result = result
        return result

    def last_result(self) -> Optional[ModelMirrorResult]:
        return self._last_result


# ── Singleton ──────────────────────────────────────────────────────────────────

_mirror: Optional[ModelMirror] = None


def get_model_mirror() -> ModelMirror:
    global _mirror
    if _mirror is None:
        _mirror = ModelMirror()
    return _mirror


GlmMirrorResult = ModelMirrorResult
GlmMirror = ModelMirror
get_glm_mirror = get_model_mirror
GLM_MIRROR_INTERVAL_HOURS = MODEL_MIRROR_INTERVAL_HOURS
GLM_GENERATE_MODEL = MODEL_MIRROR_GENERATE_MODEL
GLM_RESPONSE_LANGUAGE = MODEL_MIRROR_RESPONSE_LANGUAGE
_LOOKBACK_HOURS = MODEL_MIRROR_LOOKBACK_HOURS
