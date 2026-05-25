from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any


_SPEC_PATH = Path(__file__).resolve().parents[1] / "mcp_specs" / "responses" / "envelope.json"


def response_profile_from_args(args: dict[str, Any] | None) -> str:
    raw_args = args if isinstance(args, dict) else {}
    response_format = str(raw_args.get("response_format") or "").strip().lower()
    detail = str(raw_args.get("detail") or "").strip().lower()
    diagnostic = bool(raw_args.get("diagnostic")) or response_format == "diagnostic"
    if diagnostic:
        return "diagnostic"
    if detail == "full":
        return "full"
    return "compact"


@lru_cache(maxsize=1)
def load_response_envelope_spec() -> dict[str, Any]:
    with _SPEC_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def filter_mcp_response(
    packet: Any,
    *,
    profile: str = "compact",
    spec: dict[str, Any] | None = None,
) -> Any:
    envelope = spec or load_response_envelope_spec()
    profiles = envelope.get("profiles") if isinstance(envelope.get("profiles"), dict) else {}
    profile_spec = profiles.get(profile) if isinstance(profiles.get(profile), dict) else profiles.get("compact", {})
    included = {
        str(item)
        for item in profile_spec.get("include_visibility", ["public", "continuation"])
        if str(item).strip()
    }
    drop_empty = bool(profile_spec.get("drop_empty", profile == "compact"))
    filtered = _filter_value(packet, path=(), included=included, drop_empty=drop_empty, spec=envelope)
    if filtered is _DROP:
        return {} if isinstance(packet, dict) else []
    return filtered


class _DropSentinel:
    pass


_DROP = _DropSentinel()


def _filter_value(
    value: Any,
    *,
    path: tuple[str, ...],
    included: set[str],
    drop_empty: bool,
    spec: dict[str, Any],
) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            child_path = (*path, key_text)
            visibility = _visibility_for_path(child_path, spec)
            if visibility not in included:
                continue
            filtered = _filter_value(child, path=child_path, included=included, drop_empty=drop_empty, spec=spec)
            if filtered is _DROP:
                continue
            result[key_text] = filtered
        if drop_empty and _is_empty(result):
            return _DROP
        return result
    if isinstance(value, list):
        result_list: list[Any] = []
        for item in value:
            filtered = _filter_value(item, path=path, included=included, drop_empty=drop_empty, spec=spec)
            if filtered is _DROP:
                continue
            result_list.append(filtered)
        if drop_empty and _is_empty(result_list):
            return _DROP
        return result_list
    scalar = deepcopy(value)
    if drop_empty and _is_empty(scalar):
        return _DROP
    return scalar


def _visibility_for_path(path: tuple[str, ...], spec: dict[str, Any]) -> str:
    path_visibility = spec.get("path_visibility") if isinstance(spec.get("path_visibility"), dict) else {}
    field_visibility = spec.get("field_visibility") if isinstance(spec.get("field_visibility"), dict) else {}
    candidates = _path_candidates(path)
    for candidate in candidates:
        visibility = path_visibility.get(candidate)
        if isinstance(visibility, str) and visibility:
            return visibility
    field_name = path[-1] if path else ""
    visibility = field_visibility.get(field_name)
    if isinstance(visibility, str) and visibility:
        return visibility
    return str(spec.get("default_visibility") or "public")


def _path_candidates(path: tuple[str, ...]) -> list[str]:
    exact = ".".join(path)
    if not path:
        return []
    candidates = [exact]
    if len(path) >= 2:
        candidates.append(f"{path[-2]}.{path[-1]}")
    if len(path) >= 1:
        candidates.append(path[-1])
    return candidates


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if value == "":
        return True
    if isinstance(value, (list, dict, tuple, set)) and len(value) == 0:
        return True
    return False
