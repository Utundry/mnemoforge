from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _first_line(text: str, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:limit]
    return (text or "").strip()[:limit]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    if "```json" in raw:
        start = raw.find("```json")
        end = raw.find("```", start + 7)
        if start >= 0 and end > start:
            candidates.append(raw[start + 7:end].strip())
    if "{" in raw and "}" in raw:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidates.append(raw[start:end + 1].strip())
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _structured_result_from_response(response: str, *, write_bearing: bool) -> dict[str, Any]:
    parsed = _extract_json_object(response) if write_bearing else None
    if isinstance(parsed, dict):
        summary = str(parsed.get("summary") or "").strip() or _first_line(response) or "Packet execution completed."
        verification = str(parsed.get("verification") or "").strip()
        deliverable = parsed.get("deliverable")
        implementation_plan = parsed.get("implementation_plan")
        proposed_patch = parsed.get("proposed_patch")
        return {
            "summary": summary,
            "output_text": response,
            "verification": verification or None,
            "deliverable": deliverable,
            "implementation_plan": implementation_plan if isinstance(implementation_plan, list) else None,
            "proposed_patch": proposed_patch if isinstance(proposed_patch, list) else None,
            "structured": True,
        }
    return {
        "summary": _first_line(response) or "Packet execution completed.",
        "output_text": response,
        "verification": None,
        "deliverable": None,
        "implementation_plan": None,
        "proposed_patch": None,
        "structured": False,
    }


async def _normalize_write_bearing_response(
    *,
    response: str,
    llm_gateway,
    recommended_model: str | None,
    task_type: str | None,
    execution_mode: str,
    use_local: bool,
) -> dict[str, Any] | None:
    normalize_prompt = (
        "Convert the following worker output into one JSON object only.\n"
        "Preserve only grounded claims from the text.\n"
        "Required keys: summary, deliverable, verification, implementation_plan, proposed_patch.\n"
        "implementation_plan must be a list of short strings.\n"
        "proposed_patch must be a list of objects with keys: path, change_type, summary.\n"
        "If a field is missing, use an empty string or empty list.\n\n"
        "Worker output:\n"
        f"{response.strip()}"
    )
    normalized = await llm_gateway.generate(
        prompt=normalize_prompt,
        system="You convert packet-worker output into strict JSON.",
        task_type=task_type,
        mode=execution_mode,
        max_tokens=900,
        temperature=0.0,
        timeout=45.0,
        model_override=None if use_local else recommended_model,
        allow_local_fallback=use_local,
        prefer_local=use_local,
    )
    parsed = _structured_result_from_response(normalized, write_bearing=True)
    return parsed if parsed.get("structured") else None


def _external_model_candidates(payload: dict[str, Any], recommended_model: str | None) -> list[str]:
    candidates: list[str] = []
    if recommended_model:
        candidates.append(recommended_model)
    routing_basis = payload.get("routing_basis") or {}
    if isinstance(routing_basis, dict):
        for item in routing_basis.get("cloud_fallbacks") or []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("model_id") or "").strip()
            if model_id and model_id not in candidates:
                candidates.append(model_id)
    return candidates


def _gateway_model_candidates(
    llm_gateway,
    *,
    execution_mode: str,
    task_type: str | None,
) -> list[str]:
    candidate_fn = getattr(llm_gateway, "_candidate_models", None)
    if not callable(candidate_fn):
        return []
    try:
        return [str(item).strip() for item in candidate_fn(execution_mode, task_type=task_type) if str(item).strip()]
    except Exception:
        return []


async def _generate_packet_response(
    *,
    llm_gateway,
    prompt: str,
    system: str,
    payload: dict[str, Any],
    task_type: str | None,
    execution_mode: str,
    recommended_model: str | None,
    use_local: bool,
) -> tuple[str, str | None]:
    if use_local:
        response = await llm_gateway.generate(
            prompt=prompt,
            system=system,
            task_type=task_type,
            mode=execution_mode,
            max_tokens=1400,
            temperature=0.2,
            timeout=90.0,
            model_override=None,
            allow_local_fallback=True,
            prefer_local=True,
        )
        return response, getattr(llm_gateway, "local_model", None)

    last_error: Exception | None = None
    candidate_models: list[str] = []
    for model_id in _external_model_candidates(payload, recommended_model) + _gateway_model_candidates(
        llm_gateway,
        execution_mode=execution_mode,
        task_type=task_type,
    ):
        if model_id and model_id not in candidate_models:
            candidate_models.append(model_id)

    for model_id in candidate_models:
        try:
            response = await llm_gateway.generate(
                prompt=prompt,
                system=system,
                task_type=task_type,
                mode=execution_mode,
                max_tokens=1400,
                temperature=0.2,
                timeout=90.0,
                model_override=model_id,
                allow_local_fallback=False,
                prefer_local=False,
            )
            return response, model_id
        except Exception as exc:
            last_error = exc
            logger.warning("Packet executor model %s failed: %s", model_id, exc)

    if last_error is not None:
        try:
            response = await llm_gateway.generate(
                prompt=prompt,
                system=system,
                task_type=task_type,
                mode=execution_mode,
                max_tokens=1400,
                temperature=0.2,
                timeout=90.0,
                model_override=None,
                allow_local_fallback=True,
                prefer_local=True,
            )
            return response, getattr(llm_gateway, "local_model", None)
        except Exception as exc:
            logger.warning("Packet executor local fallback failed after cloud errors: %s", exc)
            raise last_error

    response = await llm_gateway.generate(
        prompt=prompt,
        system=system,
        task_type=task_type,
        mode=execution_mode,
        max_tokens=1400,
        temperature=0.2,
        timeout=90.0,
        model_override=None,
        allow_local_fallback=False,
        prefer_local=False,
    )
    return response, recommended_model


def build_handoff_packet_prompt(payload: dict[str, Any]) -> tuple[str, str]:
    task_description = str(payload.get("task_description") or "").strip()
    definition_of_done = str(payload.get("definition_of_done") or "").strip()
    expected_output_shape = str(payload.get("expected_output_shape") or "").strip()
    phase = str(payload.get("phase") or "").strip()
    priority = str(payload.get("priority") or "").strip()
    why_now = str(payload.get("why_now") or "").strip()
    execution_mode = str(payload.get("execution_mode") or "").strip()
    task_type = str(payload.get("task_type") or "").strip()
    phase_objective = str(payload.get("phase_objective") or "").strip()
    recommended_executor = str(payload.get("recommended_executor") or "").strip()
    recommendation_reason = str(payload.get("recommendation_reason") or "").strip()
    write_scope = [str(item).strip() for item in (payload.get("write_scope") or []) if str(item).strip()]
    core_instinct_ids = [str(item).strip() for item in (payload.get("core_instinct_ids") or []) if str(item).strip()]
    supporting_instinct_ids = [str(item).strip() for item in (payload.get("supporting_instinct_ids") or []) if str(item).strip()]
    project_context_summary = str(payload.get("project_context_summary") or "").strip()
    project_context_snapshot = str(payload.get("project_context_snapshot") or "").strip()
    project_context_refs = payload.get("project_context_refs") or {}
    routing_basis = payload.get("routing_basis") or {}
    write_bearing = bool(write_scope)

    system = (
        "You are a bounded task-packet execution worker. "
        "Produce useful work for the packet, but do not claim that files were edited, commands were run, "
        "or tests passed unless that evidence is explicitly provided in the prompt. "
        "If project or routing context is present, use it as the primary grounding material instead of guessing. "
        "If the packet is write-bearing, return a concrete proposed patch plan or candidate changes instead of pretending to merge them. "
        "Keep the answer concise and operator-friendly."
    )

    write_scope_text = ", ".join(write_scope) if write_scope else "read-only / no explicit write scope"
    routing_lines: list[str] = []
    if isinstance(routing_basis, dict):
        for key in ("task_type", "component", "tier", "score", "confidence", "reasoning"):
            raw = routing_basis.get(key)
            if raw in (None, "", [], {}):
                continue
            routing_lines.append(f"{key}: {raw}")
        fallbacks = routing_basis.get("cloud_fallbacks")
        if isinstance(fallbacks, list) and fallbacks:
            compact = ", ".join(
                str(item.get("model_id") or "").strip()
                for item in fallbacks
                if isinstance(item, dict) and str(item.get("model_id") or "").strip()
            )
            if compact:
                routing_lines.append(f"cloud_fallbacks: {compact}")
    refs_lines: list[str] = []
    if isinstance(project_context_refs, dict):
        for key, values in project_context_refs.items():
            if not values:
                continue
            compact_values = ", ".join(str(item).strip() for item in values if str(item).strip())
            if compact_values:
                refs_lines.append(f"{key}: {compact_values}")

    prompt = (
        "Execute this handoff packet.\n\n"
        f"Task: {task_description}\n"
        f"Task type: {task_type or 'unspecified'}\n"
        f"Phase: {phase or 'unspecified'}\n"
        f"Priority: {priority or 'unspecified'}\n"
        f"Why now: {why_now or 'unspecified'}\n"
        f"Execution mode: {execution_mode or 'balanced'}\n"
        f"Recommended executor: {recommended_executor or 'unspecified'}\n"
        f"Write scope: {write_scope_text}\n"
        f"Definition of done: {definition_of_done or 'Not specified'}\n"
        f"Expected output shape: {expected_output_shape or 'A concise useful result'}\n\n"
        f"Phase objective: {phase_objective or 'Not specified'}\n"
        f"Recommendation reason: {recommendation_reason or 'Not specified'}\n"
        f"Core instincts: {', '.join(core_instinct_ids) if core_instinct_ids else 'none'}\n"
        f"Supporting instincts: {', '.join(supporting_instinct_ids) if supporting_instinct_ids else 'none'}\n\n"
        "Routing basis:\n"
        f"{chr(10).join(routing_lines) if routing_lines else 'No routing metadata provided.'}\n\n"
        "Project context summary:\n"
        f"{project_context_summary or 'No compact project summary provided.'}\n\n"
        "Project context refs:\n"
        f"{chr(10).join(refs_lines) if refs_lines else 'No project context refs provided.'}\n\n"
        "Project context snapshot:\n"
        f"{project_context_snapshot or 'No project context snapshot provided.'}\n\n"
    )
    if write_bearing:
        prompt += (
            "Return one JSON object only with these keys:\n"
            "{\n"
            '  "summary": "short operator-facing summary",\n'
            '  "deliverable": "what you produced",\n'
            '  "verification": "what still needs to be checked",\n'
            '  "implementation_plan": ["step 1", "step 2"],\n'
            '  "proposed_patch": [\n'
            '    {"path": "file.py", "change_type": "update", "summary": "what to change"}\n'
            "  ]\n"
            "}\n"
            "Do not claim the patch is already applied.\n"
        )
    else:
        prompt += (
            "Return exactly these sections:\n"
            "Summary: one short paragraph describing the result\n"
            "Deliverable: the actual bounded result\n"
            "Verification: what still needs to be checked by the main agent or operator\n"
        )
    return system, prompt


async def execute_handoff_packet(payload: dict[str, Any], llm_gateway) -> dict[str, Any]:
    system, prompt = build_handoff_packet_prompt(payload)
    recommended_model = str(payload.get("recommended_model") or "").strip() or None
    recommended_executor = str(payload.get("recommended_executor") or "").strip()
    task_type = str(payload.get("task_type") or "").strip() or None
    execution_mode = str(payload.get("execution_mode") or "").strip() or "balanced"
    write_bearing = bool([str(item).strip() for item in (payload.get("write_scope") or []) if str(item).strip()])

    use_local = recommended_executor == "local_slm_background"
    response, actual_model = await _generate_packet_response(
        llm_gateway=llm_gateway,
        prompt=prompt,
        system=system,
        payload=payload,
        task_type=task_type,
        execution_mode=execution_mode,
        recommended_model=recommended_model,
        use_local=use_local,
    )

    model_used = actual_model or recommended_model or getattr(llm_gateway, "local_model", None) or "unknown"
    result = _structured_result_from_response(response, write_bearing=write_bearing)
    if write_bearing and not result.get("structured"):
        normalized = await _normalize_write_bearing_response(
            response=response,
            llm_gateway=llm_gateway,
            recommended_model=model_used,
            task_type=task_type,
            execution_mode=execution_mode,
            use_local=use_local,
        )
        if normalized is not None:
            normalized["output_text"] = response
            result = normalized
    result.update(
        {
            "model_used": model_used,
            "executor_used": recommended_executor or "background_llm",
            "task_type": task_type,
        }
    )
    return result
