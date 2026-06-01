from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.config import settings
from app.models.project_task import (
    CodeLinkRecord,
    DecisionCandidateRecord,
    RemainingRiskRecord,
    TaskCaptureCandidateRecord,
    TaskCaptureCompletionResponse,
    TaskStatementProjectionResponse,
)
from app.services.learning_store import get_learning_store, make_context_signature
from app.services.llm_gateway import get_cloud_gateway
from app.services.task_statement_service import build_task_statement_projection

logger = logging.getLogger(__name__)
from app.services.text_localization import normalize_text_for_display

_LOCAL_MODEL = os.getenv("LOCAL_GENERATE_MODEL", settings.learning_mirror_model or "qwen3:1.7b").strip() or "qwen3:1.7b"
_EMPTY_MARKERS = {"", "n/a", "none", "unknown", "not enough information", "insufficient information"}
_CAPTURE_KIND_ORDER = [
    "assumption",
    "constraint",
    "definition_of_done",
    "decision_candidate",
    "chosen_decision",
    "code_link",
    "verification_result",
    "remaining_risk",
    "result_summary",
    "handoff_summary",
]
_PATH_PATTERN = re.compile(
    r"(?P<path>(?:app|tests|docs|scripts|mcp|static|cli)/[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)(?::(?P<line>\d+(?:-\d+)?))?"
)


def _clean(value: Any, limit: int = 1200) -> str:
    return normalize_text_for_display(str(value or ""))[:limit].strip()


def _normalize_candidate_text(value: Any, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", _clean(value, limit=limit)).strip()


def _dedupe_texts(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = _normalize_candidate_text(item, limit=600)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _looks_meaningful(text: str) -> bool:
    candidate = _normalize_candidate_text(text, limit=600)
    if not candidate:
        return False
    lowered = candidate.casefold()
    if lowered in _EMPTY_MARKERS:
        return False
    if lowered.startswith("no ") or lowered.startswith("none "):
        return False
    return True


def _artifact_type_for_kind(kind: str) -> str:
    if kind in {"decision_candidate", "chosen_decision", "code_link", "remaining_risk"}:
        return kind
    return "task_capture_candidate"


def _format_code_link_candidate(*, path: str, line_range: str = "", description: str = "") -> str:
    base = path.strip()
    if line_range:
        base = f"{base}:{line_range.strip()}"
    desc = _normalize_candidate_text(description, limit=280)
    return f"{base} | {desc}" if desc else base


def _extract_code_links_from_changes(statement: TaskStatementProjectionResponse, archived: dict[str, set[str]] | None = None) -> list[TaskCaptureCandidateRecord]:
    candidates: list[TaskCaptureCandidateRecord] = []
    seen: set[str] = set()
    archived = archived or {}
    archived_code_links = archived.get("code_link", set())
    
    for change in statement.task.changes[-8:]:
        source_text = " ".join(
            part for part in [str(change.content or ""), str(change.why or "")] if str(part or "").strip()
        )
        for match in _PATH_PATTERN.finditer(source_text):
            path = match.group("path") or ""
            line_range = match.group("line") or ""
            content = _format_code_link_candidate(
                path=path,
                line_range=line_range,
                description=str(change.content or ""),
            )
            key = content.casefold()
            if not path or key in seen:
                continue
            # Skip if this code link was already rejected
            if key in archived_code_links:
                continue
            seen.add(key)
            candidates.append(
                TaskCaptureCandidateRecord(
                    kind="code_link",
                    content=content,
                    source="deterministic",
                    confidence=0.88,
                    rationale="Extracted from explicit file references present in task changes.",
                    artifact_type="code_link",
                )
            )
    return candidates


def _missing_capture_fields(statement: TaskStatementProjectionResponse) -> list[str]:
    missing: list[str] = []
    current = statement.current
    task = statement.task
    execution_changes = list(statement.diff.execution_changes or [])

    if not current.assumptions:
        missing.append("assumption")
    if not current.constraints:
        missing.append("constraint")
    if not current.definition_of_done:
        missing.append("definition_of_done")
    
    # decision_candidate: нужен везде, где уже есть chosen_decisions, чтобы
    # сохранять traceability альтернатив и rationale, включая completed tasks.
    if current.chosen_decisions and task.status != "archived":
        missing.append("decision_candidate")
    
    # code_link: требуется, если есть execution_changes (для связи с кодом)
    if execution_changes and task.status in {"active", "done"}:
        missing.append("code_link")
    
    if task.status == "done" and not current.verification:
        missing.append("verification_result")
    
    # remaining_risk: требуется для завершённых задач
    if task.status == "done":
        missing.append("remaining_risk")
    
    if execution_changes or current.chosen_decisions:
        missing.append("result_summary")
    
    if current.deferred_work or current.open_questions or task.status in {"planning", "active", "paused"}:
        missing.append("handoff_summary")
    
    return _dedupe_texts(missing)


async def _deterministic_candidates(statement: TaskStatementProjectionResponse, missing: list[str], archived: dict[str, set[str]] | None = None) -> list[TaskCaptureCandidateRecord]:
    current = statement.current
    task = statement.task
    candidates: list[TaskCaptureCandidateRecord] = []
    archived = archived or {}

    if "decision_candidate" in missing and current.chosen_decisions and task.status != "archived":
        # Generate decision_candidate from the latest chosen decision
        latest_decision = current.chosen_decisions[-1]
        content_key = _normalize_candidate_text(latest_decision, limit=600).casefold()
        # Skip if this decision was already rejected
        if "decision_candidate" not in archived or content_key not in archived.get("decision_candidate", set()):
            candidates.append(
                TaskCaptureCandidateRecord(
                    kind="decision_candidate",
                    content=latest_decision,
                    source="deterministic",
                    confidence=0.84,
                    rationale="Derived from the latest chosen decision on the task.",
                    artifact_type="decision_candidate",
                )
            )

    if "verification_result" in missing and current.verification:
        content = "; ".join(_dedupe_texts(current.verification)[:2])
        content_key = _normalize_candidate_text(content, limit=600).casefold()
        # Skip if this verification result was already rejected
        if "verification_result" not in archived or content_key not in archived.get("verification_result", set()):
            candidates.append(
                TaskCaptureCandidateRecord(
                    kind="verification_result",
                    content=content,
                    source="deterministic",
                    confidence=0.86,
                    rationale="Derived from existing verification statements already captured on the task.",
                )
            )

    if "result_summary" in missing:
        parts: list[str] = []
        if statement.diff.execution_changes:
            parts.append(statement.diff.execution_changes[-1])
        elif current.chosen_decisions:
            parts.append(f"Latest decision: {current.chosen_decisions[-1]}")
        if parts:
            content = _normalize_candidate_text(" ".join(parts), limit=600)
            content_key = content.casefold()
            # Skip if this result summary was already rejected
            if "result_summary" not in archived or content_key not in archived.get("result_summary", set()):
                candidates.append(
                    TaskCaptureCandidateRecord(
                        kind="result_summary",
                        content=content,
                        source="deterministic",
                        confidence=0.82,
                        rationale="Collapsed from the latest implementation or decision trace.",
                    )
                )

    if "handoff_summary" in missing:
        handoff_parts = [f"Objective: {current.objective}."]
        if current.chosen_decisions:
            handoff_parts.append(f"Keep decision: {current.chosen_decisions[-1]}.")
        if current.deferred_work:
            handoff_parts.append(f"Follow up on: {current.deferred_work[0]}.")
        elif current.open_questions:
            handoff_parts.append(f"Open question: {current.open_questions[0]}.")
        if task.status != "done":
            handoff_parts.append(f"Current status: {task.status}.")
        handoff = _normalize_candidate_text(" ".join(handoff_parts), limit=600)
        if _looks_meaningful(handoff):
            content_key = handoff.casefold()
            # Skip if this handoff summary was already rejected
            if "handoff_summary" not in archived or content_key not in archived.get("handoff_summary", set()):
                candidates.append(
                    TaskCaptureCandidateRecord(
                        kind="handoff_summary",
                        content=handoff,
                        source="deterministic",
                        confidence=0.8,
                        rationale="Assembled from the current statement, recent decisions, and deferred work.",
                    )
                )

    if "code_link" in missing:
        candidates.extend(_extract_code_links_from_changes(statement, archived))

    if "remaining_risk" in missing:
        risk_parts = list(_dedupe_texts(list(current.deferred_work or []) + list(current.open_questions or [])))
        if risk_parts:
            content = _normalize_candidate_text(risk_parts[0], limit=600)
            content_key = content.casefold()
            # Skip if this remaining risk was already rejected
            if "remaining_risk" not in archived or content_key not in archived.get("remaining_risk", set()):
                candidates.append(
                    TaskCaptureCandidateRecord(
                        kind="remaining_risk",
                        content=content,
                        source="deterministic",
                        confidence=0.78,
                        rationale="Derived from unresolved deferred work or open questions on a completed task.",
                        artifact_type="remaining_risk",
                    )
                )

    return [candidate for candidate in candidates if candidate.kind in missing and _looks_meaningful(candidate.content)]


def _local_prompt(statement: TaskStatementProjectionResponse, missing: list[str]) -> str:
    current = statement.current
    task = statement.task
    changes = []
    for change in task.changes[-6:]:
        detail = _normalize_candidate_text(change.content, limit=280)
        if detail:
            changes.append(f"- {change.change_type}: {detail}")
    prompt = f"""
You fill missing task-capture fields for a coding project.
Use only the grounded task data below. Do not invent implementation facts.
If a field cannot be supported by the input, return an empty list or empty string.

Task title: {task.title}
Task status: {task.status}
Objective: {current.objective}
Scope summary: {current.scope_summary}
Chosen decisions: {json.dumps(current.chosen_decisions, ensure_ascii=False)}
Deferred work: {json.dumps(current.deferred_work, ensure_ascii=False)}
Open questions: {json.dumps(current.open_questions, ensure_ascii=False)}
Existing assumptions: {json.dumps(current.assumptions, ensure_ascii=False)}
Existing constraints: {json.dumps(current.constraints, ensure_ascii=False)}
Existing definition_of_done: {json.dumps(current.definition_of_done, ensure_ascii=False)}
Existing verification: {json.dumps(current.verification, ensure_ascii=False)}
Recent task changes:
{os.linesep.join(changes) if changes else "- none"}

Return only JSON with these keys:
{json.dumps(missing, ensure_ascii=False)}

Key meanings:
- assumption -> list[str]
- constraint -> list[str]
- definition_of_done -> list[str]
- decision_candidate -> str
- chosen_decision -> str
- code_link -> str (format: path/to/file.py[:line-range] | short description)
- verification_result -> str
- remaining_risk -> str
- result_summary -> str
- handoff_summary -> str
""".strip()
    return prompt


async def _generate_local_capture_fill(ollama, prompt: str) -> str:
    return await get_cloud_gateway().generate(
        prompt,
        task_type="memory_extraction",
        mode="economy",
        max_tokens=800,
        temperature=0.0,
        timeout=45.0,
        allow_local_fallback=True,
        prefer_local=True,
    )


def _parse_local_response(raw: str, missing: list[str]) -> dict[str, list[str] | str]:
    result: dict[str, list[str] | str] = {key: [] if key in {"assumption", "constraint", "definition_of_done"} else "" for key in missing}
    text = str(raw or "").strip()
    if not text:
        return result
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        for key in missing:
            value = parsed.get(key)
            if key in {"assumption", "constraint", "definition_of_done"}:
                if isinstance(value, list):
                    result[key] = _dedupe_texts([str(item) for item in value])
                elif isinstance(value, str) and _looks_meaningful(value):
                    result[key] = [value]
            else:
                if isinstance(value, list):
                    text_value = "; ".join(_dedupe_texts([str(item) for item in value])[:2])
                    result[key] = _normalize_candidate_text(text_value, limit=600) if _looks_meaningful(text_value) else ""
                elif isinstance(value, str) and _looks_meaningful(value):
                    result[key] = _normalize_candidate_text(value, limit=600)
        return result

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        match = re.match(r"^\s*[-*]?\s*([A-Za-z_ ]+)\s*:\s*(.+?)\s*$", line)
        if not match:
            continue
        label = match.group(1).strip().lower().replace(" ", "_")
        value = _normalize_candidate_text(match.group(2), limit=600)
        if not _looks_meaningful(value):
            continue
        if label in {"assumption", "constraint", "definition_of_done"} and label in result:
            existing = list(result[label]) if isinstance(result[label], list) else []
            result[label] = _dedupe_texts(existing + [value])
        elif label in result:
            result[label] = value
    return result


def _normalize_code_link_text(value: str) -> str:
    text = _normalize_candidate_text(value, limit=600)
    if not text:
        return ""
    match = _PATH_PATTERN.search(text)
    if not match:
        return ""
    path = match.group("path") or ""
    line_range = match.group("line") or ""
    suffix = text[match.end():].strip(" |-:\t")
    return _format_code_link_candidate(path=path, line_range=line_range, description=suffix)


def _local_candidates(statement: TaskStatementProjectionResponse, missing: list[str], parsed: dict[str, list[str] | str]) -> list[TaskCaptureCandidateRecord]:
    candidates: list[TaskCaptureCandidateRecord] = []
    for kind in missing:
        value = parsed.get(kind)
        if kind in {"assumption", "constraint", "definition_of_done"}:
            items = value if isinstance(value, list) else []
            for item in _dedupe_texts([str(entry) for entry in items]):
                if not _looks_meaningful(item):
                    continue
                candidates.append(
                    TaskCaptureCandidateRecord(
                        kind=kind,
                        content=item,
                        source="local_slm",
                        confidence=0.62,
                        rationale="Completed from existing task framing with the local generation model.",
                    )
                )
        else:
            text = _normalize_candidate_text(value, limit=600) if isinstance(value, str) else ""
            if kind == "code_link":
                text = _normalize_code_link_text(text)
            if not _looks_meaningful(text):
                continue
            candidates.append(
                TaskCaptureCandidateRecord(
                    kind=kind,
                    content=text,
                    source="local_slm",
                    confidence=0.58,
                    rationale="Suggested by the local generation model from grounded task artifacts.",
                    artifact_type=_artifact_type_for_kind(kind),
                )
            )
    return candidates


async def _existing_capture_candidates(*, project: str, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    store = get_learning_store()
    for artifact_type in [
        "task_capture_candidate",
        "decision_candidate",
        "chosen_decision",
        "code_link",
        "remaining_risk",
    ]:
        rows.extend(
            await store.list_artifacts(
                artifact_type=artifact_type,
                scope="project",
                status="active",
                limit=limit,
            )
        )
    project_tag = f"project:{project}"
    task_tag = f"task_id:{task_id}"
    items: list[dict[str, Any]] = []
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if project_tag not in tags or task_tag not in tags:
            continue
        items.append(row)
    return items


async def _archived_capture_candidates(*, project: str, task_id: str, limit: int = 200) -> dict[str, set[str]]:
    """Get archived (rejected) candidates for filtering out deterministic regeneration."""
    archived: dict[str, set[str]] = {}
    store = get_learning_store()
    for artifact_type in [
        "task_capture_candidate",
        "decision_candidate",
        "chosen_decision",
        "code_link",
        "remaining_risk",
    ]:
        rows = await store.list_artifacts(
            artifact_type=artifact_type,
            scope="project",
            status="archived",
            limit=limit,
        )
        project_tag = f"project:{project}"
        task_tag = f"task_id:{task_id}"
        for row in rows:
            tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
            if project_tag not in tags or task_tag not in tags:
                continue
            kind = _extract_tag_value(tags, "capture_kind:") or artifact_type
            content_key = _normalize_candidate_text(row.get("content") or "", limit=600).casefold()
            if kind not in archived:
                archived[kind] = set()
            archived[kind].add(content_key)
    return archived


def _find_matching_existing(rows: list[dict[str, Any]], *, kind: str, content: str) -> dict[str, Any] | None:
    content_key = _normalize_candidate_text(content, limit=600).casefold()
    for row in rows:
        tags = {str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()}
        if f"capture_kind:{kind}" not in tags:
            continue
        existing_key = _normalize_candidate_text(row.get("content") or "", limit=600).casefold()
        if existing_key == content_key:
            return row
    return None


def _missing_after(statement: TaskStatementProjectionResponse, candidates: list[TaskCaptureCandidateRecord]) -> list[str]:
    filled = {candidate.kind for candidate in candidates if _looks_meaningful(candidate.content)}
    return [kind for kind in _missing_capture_fields(statement) if kind not in filled]


async def build_task_capture_completion(
    qdrant,
    ollama,
    *,
    project: str,
    task_id: str,
    persist: bool = True,
    use_local_generation: bool = True,
) -> TaskCaptureCompletionResponse:
    statement = await build_task_statement_projection(qdrant, project=project, task_id=task_id)
    missing = [kind for kind in _CAPTURE_KIND_ORDER if kind in _missing_capture_fields(statement)]
    # Get archived (rejected) candidates to prevent regeneration
    archived = await _archived_capture_candidates(project=project, task_id=task_id) if persist else {}
    candidates = await _deterministic_candidates(statement, missing, archived)
    remaining = [kind for kind in missing if kind not in {candidate.kind for candidate in candidates}]
    local_generation_used = False
    local_generation_error = ""

    if use_local_generation and remaining:
        prompt = _local_prompt(statement, remaining)
        try:
            raw = await _generate_local_capture_fill(ollama, prompt)
            parsed = _parse_local_response(raw, remaining)
            local_candidates = _local_candidates(statement, remaining, parsed)
            for candidate in local_candidates:
                if candidate.kind in {item.kind for item in candidates if item.source == "deterministic"} and candidate.kind not in {"assumption", "constraint", "definition_of_done"}:
                    continue
                candidates.append(candidate)
            local_generation_used = bool(local_candidates)
        except Exception as exc:
            local_generation_error = str(exc)[:500]
            logger.warning(
                "Task capture local generation failed for %s/%s; keeping deterministic candidates only: %s",
                project,
                task_id,
                exc,
            )

    final_candidates: list[TaskCaptureCandidateRecord] = []
    seen_pairs: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.kind, _normalize_candidate_text(candidate.content, limit=600).casefold())
        if not candidate.content or key in seen_pairs:
            continue
        seen_pairs.add(key)
        final_candidates.append(candidate)

    persisted_count = 0
    reused_count = 0
    local_existing = await _existing_capture_candidates(project=project, task_id=task_id) if persist and final_candidates else []
    generated_at = datetime.now(timezone.utc).isoformat()
    context_signature = make_context_signature(
        project=project,
        task_type="task_capture",
        phase="candidate_fill",
        category="task_capture_candidate",
        transport="api",
    )

    if persist:
        for candidate in final_candidates:
            existing = _find_matching_existing(local_existing, kind=candidate.kind, content=candidate.content)
            if existing:
                candidate.artifact_id = str(existing.get("id") or "")
                candidate.artifact_type = str(existing.get("artifact_type") or _artifact_type_for_kind(candidate.kind))
                candidate.reused_existing = True
                reused_count += 1
                continue
            artifact_type = _artifact_type_for_kind(candidate.kind)
            artifact_id = await get_learning_store().insert_artifact(
                agent_id=statement.task.agent_id or "codex",
                artifact_type=artifact_type,
                scope="project",
                status="active",
                workflow_type="task_capture",
                workflow_action="suggest_capture_artifact",
                workflow_context=json.dumps(
                    {
                        "project_id": project,
                        "task_id": task_id,
                        "kind": candidate.kind,
                        "source": candidate.source,
                        "generated_at": generated_at,
                        "task_status": statement.task.status,
                        "missing_at_generation": missing,
                        "local_model": _LOCAL_MODEL if candidate.source == "local_slm" else "",
                    },
                    ensure_ascii=False,
                ),
                content=candidate.content[:4000],
                confidence=candidate.confidence,
                evidence_count=1,
                context_signature=context_signature,
                tags=[
                    f"project:{project}",
                    f"task_id:{task_id}",
                    "task-capture-candidate",
                    f"capture_kind:{candidate.kind}",
                    f"capture_source:{candidate.source}",
                ],
                observation=candidate.rationale[:1000],
                why_it_matters="Keeps cheap task-stage capture candidates available for review and later promotion.",
            )
            candidate.artifact_id = str(artifact_id)
            candidate.artifact_type = artifact_type
            persisted_count += 1

    return TaskCaptureCompletionResponse(
        statement=statement,
        missing_before=missing,
        missing_after=_missing_after(statement, final_candidates),
        local_generation_used=local_generation_used,
        local_model=_LOCAL_MODEL if local_generation_used else "",
        local_generation_error=local_generation_error,
        persisted_count=persisted_count,
        reused_count=reused_count,
        candidates=final_candidates,
    )


# Additional artifact creation functions for MVP completion


async def create_decision_candidate(
    *,
    task_id: str,
    project: str,
    description: str,
    rationale: str,
    alternatives: list[str] | None = None,
    pros: list[str] | None = None,
    cons: list[str] | None = None,
    agent_id: str = "codex",
) -> str:
    """Создаёт кандидата на решение через learning_store."""
    store = get_learning_store()
    artifact_id = str(uuid4())
    
    content = {
        "description": description,
        "rationale": rationale,
        "alternatives": alternatives or [],
        "pros": pros or [],
        "cons": cons or [],
    }
    
    await store.upsert_candidate(
        artifact_id=artifact_id,
        agent_id=agent_id,
        artifact_type="decision_candidate",
        scope="project",
        workflow_type="task_capture",
        workflow_action="suggest_decision",
        workflow_context=json.dumps({"task_id": task_id, "project": project}),
        content=json.dumps(content),
        confidence=0.7,
        tags=[
            f"project:{project}",
            f"task_id:{task_id}",
            "task-capture-candidate",
            "capture_kind:decision_candidate",
        ],
        observation=f"Decision candidate for task {task_id}: {description[:100]}",
        why_it_matters="Captures alternative approaches for later review and selection.",
    )
    
    return artifact_id


async def create_code_link(
    *,
    task_id: str,
    project: str,
    file_path: str,
    line_range: str = "",
    description: str = "",
    change_type: str = "implementation",
    agent_id: str = "codex",
) -> str:
    """Создаёт ссылку на код через learning_store."""
    store = get_learning_store()
    artifact_id = str(uuid4())
    
    content = {
        "file_path": file_path,
        "line_range": line_range,
        "description": description,
        "change_type": change_type,
    }
    
    await store.upsert_candidate(
        artifact_id=artifact_id,
        agent_id=agent_id,
        artifact_type="code_link",
        scope="project",
        workflow_type="task_capture",
        workflow_action="record_code_link",
        workflow_context=json.dumps({"task_id": task_id, "project": project}),
        content=json.dumps(content),
        confidence=0.9,
        tags=[
            f"project:{project}",
            f"task_id:{task_id}",
            "task-capture-candidate",
            "capture_kind:code_link",
        ],
        observation=f"Code link for task {task_id}: {file_path}",
        why_it_matters="Links task execution to specific code changes for traceability.",
    )
    
    return artifact_id


async def create_remaining_risk(
    *,
    task_id: str,
    project: str,
    description: str,
    likelihood: str = "medium",
    impact: str = "medium",
    mitigation: str = "",
    owner: str | None = None,
    agent_id: str = "codex",
) -> str:
    """Создаёт запись об оставшемся риске через learning_store."""
    store = get_learning_store()
    artifact_id = str(uuid4())
    
    content = {
        "description": description,
        "likelihood": likelihood,
        "impact": impact,
        "mitigation": mitigation,
        "owner": owner,
    }
    
    await store.upsert_candidate(
        artifact_id=artifact_id,
        agent_id=agent_id,
        artifact_type="remaining_risk",
        scope="project",
        workflow_type="task_capture",
        workflow_action="record_remaining_risk",
        workflow_context=json.dumps({"task_id": task_id, "project": project}),
        content=json.dumps(content),
        confidence=0.8,
        tags=[
            f"project:{project}",
            f"task_id:{task_id}",
            "task-capture-candidate",
            "capture_kind:remaining_risk",
        ],
        observation=f"Remaining risk for task {task_id}: {description[:100]}",
        why_it_matters="Documents risks that remain after task completion for future mitigation.",
    )
    
    return artifact_id
