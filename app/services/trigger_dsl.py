"""
Trigger DSL validator for if_then_rule artifacts (MVP).

Grammar:
  TRIGGER    = CONDITION (AND CONDITION)*
  CONDITION  = ["not "] PREDICATE
  PREDICATE  = "event(" EVENT_TYPE ")"
             | "event(" EVENT_TYPE ")." FIELD OP VALUE
             | "within(" INT ", " PREDICATE ")"
  OP         = "==" | "!=" | "in"
  VALUE      = '"string"' | '["a","b",...]'

Whitelists:
  ALLOWED_EVENT_TYPES   — subset of event_type values allowed inside triggers
  ALLOWED_FIELDS        — per event_type
  ALLOWED_ACTION_TYPES  — valid action_type values for artifacts

Validation is intentionally strict: reject anything not on the whitelist
so GLM-produced candidates cannot inject arbitrary code/field access.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ── Whitelists ─────────────────────────────────────────────────────────────────

ALLOWED_EVENT_TYPES: frozenset[str] = frozenset({
    "user_request",
    "user_feedback",
    "dialogue_excerpt",
    "dialogue_signal",
    "tool_call",
    "tool_result",
    "memory_write",
    "episode_end",
    "artifact_suggested",
})

ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "user_request":      frozenset({"request_type", "proposal_type"}),
    "user_feedback":     frozenset({"valence"}),
    "dialogue_excerpt":  frozenset({"source_path", "transport"}),
    "dialogue_signal":   frozenset({"signal_type", "transport"}),
    "tool_call":         frozenset({"tool_name", "duration_s"}),
    "tool_result":       frozenset({"tool_name", "success", "empty"}),
    "memory_write":      frozenset({"category", "agent_id"}),
    "episode_end":       frozenset(),
    "artifact_suggested": frozenset({"action_type"}),
}

ALLOWED_ACTION_TYPES: frozenset[str] = frozenset({
    "auto_save_result",
    "suggest_save_result",
    "run_tests",
    "suggest_run_tests",
    "create_improvement",
    "suggest_create_improvement",
    "rebuild_docs",
    "suggest_rebuild_docs",
    "request_missing_info",
    "switch_to_background_job",
    "crystallize_knowledge",
})

# ── Patterns ───────────────────────────────────────────────────────────────────

_RE_EVENT_SIMPLE   = re.compile(r'^event\((\w+)\)$')
_RE_EVENT_FIELD    = re.compile(r'^event\((\w+)\)\.(\w+)\s*(==|!=|in)\s*(.+)$')
_RE_WITHIN         = re.compile(r'^within\((\d+),\s*(.+)\)$')
_RE_STRING_VALUE   = re.compile(r'^"[^"]*"$')
_RE_LIST_VALUE     = re.compile(r'^\[.*\]$')


@dataclass
class ValidationError:
    message: str


def validate_trigger(trigger: str) -> list[str]:
    """
    Validate a trigger DSL string.
    Returns a list of error messages (empty → valid).
    """
    if not trigger or not trigger.strip():
        return ["trigger must not be empty"]

    errors: list[str] = []
    conditions = [c.strip() for c in re.split(r'\band\b', trigger, flags=re.IGNORECASE)]

    for raw in conditions:
        errs = _validate_condition(raw)
        errors.extend(errs)

    return errors


def _validate_condition(raw: str) -> list[str]:
    errors: list[str] = []
    cond = raw.strip()

    # Strip leading "not "
    negated = cond.lower().startswith("not ")
    if negated:
        cond = cond[4:].strip()

    # within(SECONDS, PREDICATE)
    m_within = _RE_WITHIN.match(cond)
    if m_within:
        seconds_str, inner = m_within.group(1), m_within.group(2).strip()
        try:
            seconds = int(seconds_str)
            if seconds <= 0:
                errors.append(f"within() seconds must be positive, got {seconds}")
        except ValueError:
            errors.append(f"within() first arg must be integer, got '{seconds_str}'")
        errors.extend(_validate_predicate(inner))
        return errors

    errors.extend(_validate_predicate(cond))
    return errors


def _validate_predicate(pred: str) -> list[str]:
    errors: list[str] = []

    # event(TYPE)
    m_simple = _RE_EVENT_SIMPLE.match(pred)
    if m_simple:
        event_type = m_simple.group(1)
        if event_type not in ALLOWED_EVENT_TYPES:
            errors.append(
                f"unknown event_type '{event_type}'; allowed: {sorted(ALLOWED_EVENT_TYPES)}"
            )
        return errors

    # event(TYPE).field OP value
    m_field = _RE_EVENT_FIELD.match(pred)
    if m_field:
        event_type, field, op, value_str = (
            m_field.group(1), m_field.group(2),
            m_field.group(3), m_field.group(4).strip(),
        )
        if event_type not in ALLOWED_EVENT_TYPES:
            errors.append(
                f"unknown event_type '{event_type}'; allowed: {sorted(ALLOWED_EVENT_TYPES)}"
            )
        else:
            allowed_fields = ALLOWED_FIELDS.get(event_type, frozenset())
            if field not in allowed_fields:
                errors.append(
                    f"field '{field}' not allowed for event_type '{event_type}'; "
                    f"allowed: {sorted(allowed_fields) or '(none)'}"
                )
        errors.extend(_validate_value(op, value_str))
        return errors

    errors.append(f"unrecognised predicate: '{pred}'")
    return errors


def _validate_value(op: str, value_str: str) -> list[str]:
    errors: list[str] = []
    if op == "in":
        if not _RE_LIST_VALUE.match(value_str):
            errors.append(f"'in' operator requires a JSON list, got: '{value_str}'")
        else:
            try:
                parsed = json.loads(value_str)
                if not isinstance(parsed, list):
                    errors.append(f"'in' value must be a JSON array, got: {type(parsed)}")
            except json.JSONDecodeError as exc:
                errors.append(f"invalid JSON list in 'in' clause: {exc}")
    else:
        if not _RE_STRING_VALUE.match(value_str):
            errors.append(
                f"'{op}' operator requires a quoted string value, got: '{value_str}'"
            )
    return errors


def is_valid_action_type(action_type: str) -> bool:
    return action_type in ALLOWED_ACTION_TYPES


def validate_if_then_rule(trigger: str, action_type: str) -> list[str]:
    """Full validation for an if_then_rule artifact."""
    errors = validate_trigger(trigger)
    if action_type not in ALLOWED_ACTION_TYPES:
        errors.append(
            f"unknown action_type '{action_type}'; allowed: {sorted(ALLOWED_ACTION_TYPES)}"
        )
    return errors
