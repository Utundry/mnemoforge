from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping, TypeVar


T = TypeVar("T")


def build_candidate_revision(
    *,
    base: Mapping[str, Any],
    updates: Mapping[str, Any],
    fields: Iterable[str],
    proposed_at: Any,
    status: str = "proposed",
    proposed_at_field: str = "proposed_at",
) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for field in fields:
        if field in updates and updates[field] is not None:
            candidate[field] = deepcopy(updates[field])
        else:
            candidate[field] = deepcopy(base.get(field))
    candidate["status"] = status
    candidate[proposed_at_field] = proposed_at
    return candidate


def apply_candidate_fields(
    *,
    effective: Mapping[str, Any],
    candidate: Mapping[str, Any],
    fields: Iterable[str],
) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    for field in fields:
        if field in candidate and candidate[field] is not None:
            applied[field] = deepcopy(candidate[field])
        else:
            applied[field] = deepcopy(effective.get(field))
    return applied


def extract_prefixed_candidate(
    payload: Mapping[str, Any],
    *,
    fields: Iterable[str],
    prefix: str = "candidate_",
    required_field: str,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    marker = payload.get(f"{prefix}{required_field}")
    if marker in (None, "", [], {}):
        return None
    candidate: dict[str, Any] = {}
    for field in fields:
        value = payload.get(f"{prefix}{field}")
        if value is None and defaults and field in defaults:
            value = defaults[field]
        candidate[field] = deepcopy(value)
    return candidate


def prefixed_candidate_patch(
    candidate: Mapping[str, Any],
    *,
    fields: Iterable[str],
    prefix: str = "candidate_",
) -> dict[str, Any]:
    return {
        f"{prefix}{field}": deepcopy(candidate.get(field))
        for field in fields
    }


def clear_prefixed_candidate_patch(
    *,
    fields: Iterable[str],
    prefix: str = "candidate_",
) -> dict[str, Any]:
    return {f"{prefix}{field}": None for field in fields}


def stage_buffered_revision(
    *,
    effective_value: T,
    effective_updated_at: Any,
    replacement_value: T,
    replacement_updated_at: Any,
    preserve_effective: bool,
    empty_factory: Callable[[], T],
) -> tuple[T, Any, T, Any]:
    if preserve_effective:
        return (
            deepcopy(effective_value),
            effective_updated_at,
            deepcopy(replacement_value),
            replacement_updated_at,
        )
    return (
        deepcopy(replacement_value),
        replacement_updated_at,
        empty_factory(),
        None,
    )


def apply_buffered_revision(
    *,
    effective_value: T,
    effective_updated_at: Any,
    candidate_value: T,
    candidate_updated_at: Any,
    empty_factory: Callable[[], T],
) -> tuple[T, Any, T, Any]:
    if not candidate_value:
        raise ValueError("No candidate revision")
    return (
        deepcopy(candidate_value),
        candidate_updated_at or effective_updated_at,
        empty_factory(),
        None,
    )


def discard_buffered_revision(
    *,
    effective_value: T,
    effective_updated_at: Any,
    candidate_value: T,
    empty_factory: Callable[[], T],
) -> tuple[T, Any, T, Any]:
    if not candidate_value:
        raise ValueError("No candidate revision")
    return (
        deepcopy(effective_value),
        effective_updated_at,
        empty_factory(),
        None,
    )
