from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.models.stenographer import STENOGRAPHER_KIND_PATTERN


_VALID_SPAN_KINDS = set(STENOGRAPHER_KIND_PATTERN.strip("^$()").split("|"))
_START_RE = re.compile(r"\[stenographer:start(?P<attrs>[^\]]*)\]", re.IGNORECASE)
_STOP_RE = re.compile(r"\[stenographer:stop\]", re.IGNORECASE)
_ATTR_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)=(?P<value>\"[^\"]*\"|'[^']*'|[^\s\]]+)")
_TEXT_FIELDS = ("summary", "result", "content")


@dataclass(frozen=True)
class ParsedStenographerSpan:
    kind: str
    content: str
    task_id: str = ""
    work_id: str = ""


def _unquote_attr(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'\"', "'"}:
        return text[1:-1].strip()
    return text


def _attrs(raw: str) -> dict[str, str]:
    return {match.group("key").strip(): _unquote_attr(match.group("value")) for match in _ATTR_RE.finditer(raw or "")}


def parse_stenographer_tagged_spans(text: Any, *, task_id: str = "", work_id: str = "") -> list[ParsedStenographerSpan]:
    source = str(text or "")
    if "[stenographer:start" not in source.lower():
        return []
    scoped_task_id = str(task_id or "").strip()
    scoped_work_id = str(work_id or "").strip()
    spans: list[ParsedStenographerSpan] = []
    pos = 0
    while True:
        start = _START_RE.search(source, pos)
        if not start:
            break
        stop = _STOP_RE.search(source, start.end())
        if not stop:
            break
        attrs = _attrs(start.group("attrs"))
        kind = str(attrs.get("kind") or "").strip()
        span_task_id = str(attrs.get("task_id") or "").strip()
        span_work_id = str(attrs.get("work_id") or "").strip()
        content = source[start.end() : stop.start()].strip()
        if (
            kind in _VALID_SPAN_KINDS
            and content
            and (not scoped_task_id or not span_task_id or span_task_id == scoped_task_id)
            and (not scoped_work_id or not span_work_id or span_work_id == scoped_work_id)
        ):
            spans.append(
                ParsedStenographerSpan(
                    kind=kind,
                    content=content,
                    task_id=span_task_id,
                    work_id=span_work_id,
                )
            )
        pos = stop.end()
    return spans


def _payload_text_values(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field in _TEXT_FIELDS:
        value = payload.get(field)
        if isinstance(value, list):
            values.extend(str(item) for item in value if str(item or "").strip())
        elif value not in (None, ""):
            values.append(str(value))
    return values


def record_tagged_spans_from_payload(
    *,
    store: Any,
    payload: dict[str, Any],
    project: str,
    task_id: str,
    work_id: str,
    agent_id: str,
    session_id: str,
    source: str,
) -> int:
    count = 0
    for text in _payload_text_values(payload):
        for span in parse_stenographer_tagged_spans(text, task_id=task_id, work_id=work_id):
            store.record_span(
                project=project,
                task_id=task_id,
                work_id=work_id,
                agent_id=agent_id,
                session_id=session_id,
                kind=span.kind,
                source=source,
                content=span.content,
            )
            count += 1
    return count

