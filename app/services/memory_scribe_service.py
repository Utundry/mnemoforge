from __future__ import annotations

import json
import re
from typing import Any

from app.services.project_tasks_content import build_task_checkpoint_content
from app.services.replay_completeness_service import build_token_budget


_SECTION_ALIASES = {
    "decision": "decisions",
    "decisions": "decisions",
    "changed": "changed_files",
    "changed files": "changed_files",
    "files": "changed_files",
    "verification": "verification",
    "verified": "verification",
    "risk": "remaining_risk",
    "risks": "remaining_risk",
    "remaining risk": "remaining_risk",
    "next": "next_step",
    "next step": "next_step",
    "summary": "summary",
}


def _clip(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _clean_list(value: Any, *, limit: int = 8, item_limit: int = 260) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"(?:\n|;|\u2022|- )+", value)
    elif isinstance(value, list):
        raw_items = []
        for item in value:
            if isinstance(item, str):
                raw_items.extend(re.split(r"(?:\n|;|\u2022|- )+", item))
            else:
                raw_items.append(item)
    else:
        raw_items = [value]
    items: list[str] = []
    for item in raw_items:
        text = _clip(item, item_limit)
        if not text or text in items:
            continue
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return {}
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _extract_labeled_notes(raw_notes: str) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    current_key = ""
    for line in str(raw_notes or "").splitlines():
        text = line.strip()
        if not text:
            continue
        match = re.match(r"^([A-Za-z _-]{3,32})\s*:\s*(.+)$", text)
        if match:
            label = match.group(1).strip().lower().replace("_", " ")
            key = _SECTION_ALIASES.get(label)
            if key:
                current_key = key
                value = match.group(2).strip()
                if key == "summary" or key == "next_step":
                    extracted[key] = value
                else:
                    extracted.setdefault(key, []).append(value)
                continue
        if current_key and current_key not in {"summary", "next_step"}:
            extracted.setdefault(current_key, []).append(text)
    return extracted


def _fallback_summary(raw_notes: str, task_title: str = "") -> str:
    lines = [line.strip(" -\t") for line in str(raw_notes or "").splitlines() if line.strip()]
    for line in lines:
        if len(line) >= 20 and not re.match(r"^[A-Za-z _-]{3,32}\s*:", line):
            return _clip(line, 420)
    if task_title:
        return f"Captured work progress for {task_title}."
    return "Captured work progress from raw notes."


def _normalize_draft(
    draft: dict[str, Any],
    *,
    project: str,
    task_id: str,
    task_title: str,
    stage: str,
    status: str,
    raw_notes: str,
) -> dict[str, Any]:
    labeled = _extract_labeled_notes(raw_notes)
    summary = _clip(draft.get("summary") or labeled.get("summary") or _fallback_summary(raw_notes, task_title), 520)
    next_step = _clip(draft.get("next_step") or labeled.get("next_step") or "", 360)
    decisions = _clean_list(draft.get("decisions") or labeled.get("decisions"), limit=8)
    changed_files = _clean_list(draft.get("changed_files") or labeled.get("changed_files"), limit=16, item_limit=180)
    verification = _clean_list(draft.get("verification") or labeled.get("verification"), limit=8)
    remaining_risk = _clean_list(draft.get("remaining_risk") or labeled.get("remaining_risk"), limit=8)
    blockers = _clean_list(draft.get("blockers") or labeled.get("blockers"), limit=8)
    return {
        "project": project,
        "task_id": task_id,
        "task_title": task_title,
        "stage": stage,
        "status": status,
        "summary": summary,
        "blockers": blockers,
        "decisions": decisions,
        "changed_files": changed_files,
        "verification": verification,
        "remaining_risk": remaining_risk,
        "next_step": next_step,
    }


def evaluate_scribe_quality(draft: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    if not str(draft.get("summary") or "").strip():
        missing.append("summary")
    if not draft.get("verification"):
        missing.append("verification")
    if not str(draft.get("next_step") or "").strip():
        missing.append("next_step")
    useful_optional = sum(1 for key in ("decisions", "changed_files", "remaining_risk", "blockers") if draft.get(key))
    if useful_optional == 0:
        missing.append("specific_evidence")
    status = "ready" if not missing else "needs_review"
    confidence = 0.9 if status == "ready" else max(0.35, 0.75 - 0.12 * len(missing))
    return {
        "status": status,
        "confidence": round(confidence, 2),
        "missing": missing,
        "can_autofill_checkpoint": status == "ready",
        "requires_reasoning_model_review": status != "ready",
    }


def build_scribe_prompt(payload: dict[str, Any]) -> str:
    return (
        "Convert the raw agent work notes into a compact SuperMemory task checkpoint draft.\n"
        "Return JSON only with keys: summary, blockers, decisions, changed_files, verification, remaining_risk, next_step.\n"
        "Rules: preserve concrete facts; do not invent files, tests, or decisions; use empty arrays when evidence is absent; keep each item short.\n\n"
        f"Project: {payload.get('project') or 'supermemory'}\n"
        f"Task id: {payload.get('task_id') or ''}\n"
        f"Task title: {payload.get('task_title') or ''}\n"
        f"Stage: {payload.get('stage') or 'in_progress'}\n"
        f"Status: {payload.get('status') or 'active'}\n\n"
        "Raw notes:\n"
        f"{str(payload.get('raw_notes') or '')[:12000]}"
    )


async def compact_memory_scribe(payload: dict[str, Any], llm_gateway: Any | None = None) -> dict[str, Any]:
    project = str(payload.get("project") or "supermemory").strip() or "supermemory"
    task_id = str(payload.get("task_id") or "").strip()
    task_title = str(payload.get("task_title") or "").strip()
    stage = str(payload.get("stage") or "in_progress").strip().lower() or "in_progress"
    status = str(payload.get("status") or "active").strip().lower() or "active"
    raw_notes = str(payload.get("raw_notes") or "").strip()
    model_context_window = int(payload.get("model_context_window") or 32_000)
    if not raw_notes:
        raise ValueError("memory scribe requires raw_notes")

    prompt = build_scribe_prompt(payload)
    parsed: dict[str, Any] = {}
    scribe_model = "deterministic_fallback"
    scribe_error = ""
    if llm_gateway is not None and bool(payload.get("use_llm", True)):
        try:
            response = await llm_gateway.generate(
                prompt,
                system="You are a low-cost memory scribe. Extract concise factual checkpoint fields as JSON.",
                task_type="memory_extraction",
                mode=str(payload.get("mode") or "economy"),
                max_tokens=int(payload.get("max_tokens") or 700),
                temperature=0.0,
                timeout=float(payload.get("timeout") or 45.0),
                allow_local_fallback=True,
                prefer_local=True,
            )
            parsed = _parse_json_object(response)
            scribe_model = "llm_gateway"
        except Exception as exc:
            scribe_error = str(exc)

    draft = _normalize_draft(
        parsed,
        project=project,
        task_id=task_id,
        task_title=task_title,
        stage=stage,
        status=status,
        raw_notes=raw_notes,
    )
    quality = evaluate_scribe_quality(draft)
    checkpoint_content = build_task_checkpoint_content(
        stage=stage,
        status=status,
        summary=draft["summary"],
        blockers=draft["blockers"],
        decisions=draft["decisions"],
        changed_files=draft["changed_files"],
        verification=draft["verification"],
        remaining_risk=draft["remaining_risk"],
        next_step=draft["next_step"],
        reason=str(payload.get("reason") or "memory_scribe_compact").strip(),
    )
    result = {
        "project": project,
        "task_id": task_id,
        "draft": draft,
        "checkpoint_content": checkpoint_content,
        "quality_gate": quality,
        "scribe": {
            "source": scribe_model,
            "mode": str(payload.get("mode") or "economy"),
            "mutates_memory": False,
            "error": scribe_error,
        },
    }
    result["token_budget"] = build_token_budget(
        response_chars=len(json.dumps(result, ensure_ascii=False, default=str)),
        model_context_window=model_context_window,
        resume_budget_profile=str(payload.get("resume_budget_profile") or "normal"),
        resume_budget_ratio=payload.get("resume_budget_ratio"),
        overflow_reason="Memory scribe draft preserves checkpoint fields and can be reviewed before persistence.",
    )
    return result
