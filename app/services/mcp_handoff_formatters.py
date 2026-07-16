from __future__ import annotations

import json
import re
from typing import Any


def _build_handoff_context_refs(enrich_data: dict[str, Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    laws = [str(item.get("id") or "").strip() for item in enrich_data.get("laws") or [] if str(item.get("id") or "").strip()]
    if laws:
        refs["laws"] = laws[:10]
    components = [str(item.get("component_id") or "").strip() for item in enrich_data.get("components") or [] if str(item.get("component_id") or "").strip()]
    if components:
        refs["components"] = components[:10]
    improvements = [str(item.get("id") or "").strip() for item in enrich_data.get("improvements") or [] if str(item.get("id") or "").strip()]
    if improvements:
        refs["improvements"] = improvements[:10]
    runtime_hints = [str(item.get("id") or "").strip() for item in enrich_data.get("runtime_hints") or [] if str(item.get("id") or "").strip()]
    if runtime_hints:
        refs["runtime_hints"] = runtime_hints[:10]
    tasks = [str(item.get("task_id") or "").strip() for item in enrich_data.get("tasks") or [] if str(item.get("task_id") or "").strip()]
    if tasks:
        refs["tasks"] = tasks[:10]
    task_capture_candidates = [str(item.get("artifact_id") or "").strip() for item in enrich_data.get("task_capture_candidates") or [] if str(item.get("artifact_id") or "").strip()]
    if task_capture_candidates:
        refs["task_capture_candidates"] = task_capture_candidates[:10]
    docs_sections = [str(item.get("section_key") or "").strip() for item in enrich_data.get("docs_sections") or [] if str(item.get("section_key") or "").strip()]
    if docs_sections:
        refs["docs_sections"] = docs_sections[:10]
    return refs


def _build_handoff_context_summary(enrich_data: dict[str, Any]) -> str:
    coverage = []
    for key in ("laws", "components", "improvements", "runtime_hints", "tasks", "task_capture_candidates", "docs_sections"):
        count = len(enrich_data.get(key) or [])
        if count:
            coverage.append(f"{key}={count}")
    highlights: list[str] = []
    laws = enrich_data.get("laws") or []
    if laws:
        titles = [str(item.get("title") or "").strip() for item in laws[:2] if str(item.get("title") or "").strip()]
        if titles:
            highlights.append("laws: " + ", ".join(titles))
    components = enrich_data.get("components") or []
    if components:
        names = [str(item.get("name") or item.get("component_id") or "").strip() for item in components[:2] if str(item.get("name") or item.get("component_id") or "").strip()]
        if names:
            highlights.append("components: " + ", ".join(names))
    improvements = enrich_data.get("improvements") or []
    if improvements:
        titles = [str(item.get("title") or "").strip() for item in improvements[:2] if str(item.get("title") or "").strip()]
        if titles:
            highlights.append("improvements: " + ", ".join(titles))
    task_triage = enrich_data.get("task_triage") or {}
    recommended_task_id = str(task_triage.get("recommended_task_id") or "").strip()
    if recommended_task_id:
        highlights.append("next_task: " + recommended_task_id)
    task_capture_candidates = enrich_data.get("task_capture_candidates") or []
    if task_capture_candidates:
        labels = []
        for item in task_capture_candidates[:2]:
            kind = str(item.get("kind") or "draft").strip()
            task_id = str(item.get("task_id") or "").strip()
            labels.append(f"{kind}@{task_id}" if task_id else kind)
        if labels:
            highlights.append("capture_drafts: " + ", ".join(labels))
    parts: list[str] = []
    if coverage:
        parts.append("coverage " + ", ".join(coverage))
    if highlights:
        parts.append("highlights " + " | ".join(highlights))
    if enrich_data.get("code_inspection_recommended"):
        parts.append("code inspection fallback recommended")
    return "; ".join(parts)[:2000]


def _summarize_handoff_ref_counts(refs: dict[str, list[str]]) -> str:
    parts = [f"{key}={len(values)}" for key, values in refs.items() if values]
    return ", ".join(parts)


def _summarize_handoff_bucket_counts(values: dict[str, Any]) -> str:
    return ", ".join(f"{key}={count}" for key, count in values.items() if count)


def _format_handoff_merge_back_guidance(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()[:180]
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "; ".join(items[:3])[:180]
    if isinstance(value, dict):
        priority_keys = ("summary", "action", "reason", "guidance", "target", "next_step")
        parts: list[str] = []
        for key in priority_keys:
            raw = value.get(key)
            if raw not in (None, "", [], {}):
                parts.append(f"{key}={raw}")
        if not parts:
            for key, raw in value.items():
                if raw not in (None, "", [], {}):
                    parts.append(f"{key}={raw}")
                if len(parts) >= 3:
                    break
        return "; ".join(parts)[:180]
    return str(value).strip()[:180]


def _format_handoff_scope(scope: Any) -> str:
    if isinstance(scope, str):
        return scope.strip()
    if isinstance(scope, list):
        values = [str(item).strip() for item in scope if str(item).strip()]
        return ", ".join(values)
    return str(scope).strip()


def _format_handoff_background_payload(payload: Any) -> str:
    if payload in (None, "", [], {}):
        return ""
    if isinstance(payload, str):
        return payload.strip()[:240]
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:240]
    except Exception:
        return str(payload).strip()[:240]


def _append_handoff_background_state(parts: list[str], item: dict[str, Any]) -> None:
    if item.get("background_job_status"):
        parts.append(f"background_job_status={item['background_job_status']}")
    if item.get("dispatched_job_id"):
        parts.append(f"dispatched_job_id={item['dispatched_job_id']}")


def _extract_handoff_field(item: dict[str, Any], field: str) -> Any:
    value = item.get(field)
    if value not in (None, "", []):
        return value
    content = item.get("content") or ""
    prefix = f"{field}:"
    for line in content.splitlines():
        if line.startswith(prefix):
            raw = line[len(prefix):].strip()
            if field == "write_scope":
                return [part.strip() for part in raw.split(",") if part.strip()]
            return raw
    return None


def _sanitize_handoff_content_preview(content: str) -> str:
    filtered: list[str] = []
    skip_prefixes = (
        "project_context_summary:",
        "project_context_refs:",
        "project_context_snapshot:",
        "project_id:",
        "phase:",
        "priority:",
        "definition_of_done:",
        "expected_output_shape:",
        "phase_objective:",
        "owner_agent:",
        "write_scope:",
        "executor_used:",
        "model_used:",
        "execution_mode:",
        "background_job_type:",
        "background_payload:",
        "core_instinct_ids:",
        "supporting_instinct_ids:",
    )
    for line in (content or "").splitlines():
        if any(line.startswith(prefix) for prefix in skip_prefixes):
            continue
        filtered.append(line)
    return "\n".join(filtered)[:800]


def _format_handoff_workspace_summary(data: dict[str, Any]) -> str:
    agent_id = data.get("agent_id") or "unknown"
    statuses = ", ".join(data.get("statuses") or ["all"])
    lines = [f"Workspace handoff summary for '{agent_id}' ({statuses}):"]
    if data.get("handoff_label"):
        lines.append(f"handoff_label: {data['handoff_label']}")
    if data.get("owner_agent"):
        lines.append(f"owner_agent: {data['owner_agent']}")
    if data.get("write_scope"):
        lines.append(f"write_scope: {_format_handoff_scope(data['write_scope'])}")
    lines.append(f"total: {data.get('total', 0)}")
    if data.get("by_status"):
        lines.append("by_status: " + _summarize_handoff_bucket_counts(data["by_status"]))
    if data.get("by_owner_agent"):
        lines.append("by_owner_agent: " + _summarize_handoff_bucket_counts(data["by_owner_agent"]))
    if data.get("by_phase"):
        lines.append("by_phase: " + _summarize_handoff_bucket_counts(data["by_phase"]))
    if data.get("by_execution_mode"):
        lines.append("by_execution_mode: " + _summarize_handoff_bucket_counts(data["by_execution_mode"]))
    if data.get("by_executor_used"):
        lines.append("by_executor_used: " + _summarize_handoff_bucket_counts(data["by_executor_used"]))
    if data.get("merge_back_guidance"):
        lines.append(f"merge_back_guidance: {_format_handoff_merge_back_guidance(data['merge_back_guidance'])}")
    parallel = data.get("parallel_execution") or {}
    if parallel:
        lines.append(
            "parallel_execution: "
            + f"running={int(parallel.get('running_count', 0))}, "
            + f"planned={int(parallel.get('planned_packet_count', 0))}, "
            + f"blocked={int(parallel.get('blocked_count', 0))}, "
            + f"waves={len(parallel.get('waves') or [])}"
        )
        waves = parallel.get("waves") or []
        for wave in waves[:3]:
            lines.append(
                f"- wave {wave.get('wave')}: packets={wave.get('packet_count', 0)} "
                f"write_scope={_format_handoff_scope(wave.get('write_scope_union') or [])}"
            )
        if len(waves) > 3:
            lines.append(f"- ... {len(waves) - 3} more waves")
        blocked = parallel.get("blocked_packets") or []
        if blocked:
            lines.append(f"parallel_blocked_packets: {len(blocked)}")
    recent_packets = data.get("recent_packets") or []
    if recent_packets:
        lines.append("recent_packets:")
        for item in recent_packets:
            parts = []
            task_id = item.get("task_id") or "unknown"
            label = item.get("handoff_label")
            status = item.get("status") or "unknown"
            owner = item.get("owner_agent") or "unassigned"
            phase = item.get("phase") or "unspecified"
            priority = item.get("priority") or "unspecified"
            memory_id = item.get("memory_id") or "unknown"
            executor_used = item.get("executor_used")
            model_used = item.get("model_used")
            parts.append(f"task_id={task_id}")
            if label:
                parts.append(f"label={label}")
            parts.extend(
                [
                    f"status={status}",
                    f"owner_agent={owner}",
                    f"phase={phase}",
                    f"priority={priority}",
                    f"memory_id={memory_id}",
                ]
            )
            if executor_used:
                parts.append(f"executor_used={executor_used}")
            if model_used:
                parts.append(f"model_used={model_used}")
            if item.get("execution_mode"):
                parts.append(f"execution_mode={item['execution_mode']}")
            if item.get("background_job_type"):
                parts.append(f"background_job_type={item['background_job_type']}")
            if item.get("background_payload"):
                parts.append(f"background_payload={_format_handoff_background_payload(item['background_payload'])}")
            _append_handoff_background_state(parts, item)
            if item.get("project_context_ref_counts"):
                parts.append(f"refs={item['project_context_ref_counts']}")
            lines.append("- " + " ".join(parts))
    if data.get("pending_labels"):
        lines.append(f"pending_labels: {len(data['pending_labels'])}")
    return "\n".join(lines)


def _format_handoff_decomposition(data: dict[str, Any]) -> str:
    lines = ["Task packet decomposition:"]
    if data.get("project_id"):
        lines.append(f"project_id: {data['project_id']}")
    lines.append(f"strategy: {data.get('strategy') or 'unknown'}")
    lines.append(f"recommended_packet_count: {data.get('recommended_packet_count', 0)}")
    if data.get("phase"):
        lines.append(f"phase: {data['phase']}")
    if data.get("phase_objective"):
        lines.append(f"phase_objective: {data['phase_objective']}")
    if data.get("why_split"):
        lines.append(f"why_split: {data['why_split']}")
    packets = data.get("packets") or []
    if packets:
        lines.append("packets:")
        for item in packets:
            parts = [f"label={item.get('handoff_label') or 'packet'}"]
            if item.get("owner_agent"):
                parts.append(f"owner_agent={item['owner_agent']}")
            parts.append(f"phase={item.get('phase') or 'unspecified'}")
            parts.append(f"priority={item.get('priority') or 'medium'}")
            if item.get("execution_mode"):
                parts.append(f"execution_mode={item['execution_mode']}")
            if item.get("suggested_execution_tier"):
                parts.append(f"suggested_execution_tier={item['suggested_execution_tier']}")
            if item.get("background_job_type"):
                parts.append(f"background_job_type={item['background_job_type']}")
            if item.get("background_payload"):
                parts.append(f"background_payload={_format_handoff_background_payload(item['background_payload'])}")
            _append_handoff_background_state(parts, item)
            if item.get("model_hint"):
                parts.append(f"model_hint={item['model_hint']}")
            if item.get("write_scope"):
                parts.append(f"write_scope={_format_handoff_scope(item['write_scope'])}")
            if item.get("executor_used"):
                parts.append(f"executor_used={item['executor_used']}")
            if item.get("model_used"):
                parts.append(f"model_used={item['model_used']}")
            lines.append("- " + " ".join(parts))
            if item.get("definition_of_done"):
                lines.append(f"  done: {item['definition_of_done']}")
            if item.get("expected_output_shape"):
                lines.append(f"  output: {item['expected_output_shape']}")
    return "\n".join(lines)


def _format_created_task_packets(data: dict[str, Any]) -> str:
    packets = data.get("created_packets") or data.get("packets") or []
    created_count = data.get("created_count")
    if created_count is None:
        created_count = len(packets)
    lines = [f"Created {created_count} task packet(s)"]
    if data.get("project_id"):
        lines.append(f"project_id: {data['project_id']}")
    if data.get("task_description"):
        lines.append(f"task_description: {data['task_description']}")
    if data.get("reason"):
        lines.append(f"reason: {data['reason']}")
    if data.get("from_model_id"):
        lines.append(f"from_model_id: {data['from_model_id']}")
    if data.get("partial_result"):
        lines.append(f"partial_result: {data['partial_result'][:500]}")
    if data.get("key_facts"):
        lines.append("key_facts: " + ", ".join(str(item) for item in data.get("key_facts") or []))
    for i, packet in enumerate(packets, 1):
        lines.append(f"\n--- Packet {i} ---")
        if packet.get("task_id"):
            lines.append(f"task_id: {packet['task_id']}")
        if packet.get("handoff_label"):
            lines.append(f"handoff_label: {packet['handoff_label']}")
        if packet.get("memory_id"):
            lines.append(f"memory_id: {packet['memory_id']}")
        if packet.get("to_agent"):
            lines.append(f"to: {packet['to_agent']}")
        if packet.get("status"):
            lines.append(f"status: {packet['status']}")
        if packet.get("owner_agent"):
            lines.append(f"owner_agent: {packet['owner_agent']}")
        if packet.get("write_scope"):
            lines.append(f"write_scope: {_format_handoff_scope(packet['write_scope'])}")
        if packet.get("phase"):
            lines.append(f"phase: {packet['phase']}")
        if packet.get("priority"):
            lines.append(f"priority: {packet['priority']}")
        if packet.get("execution_mode"):
            lines.append(f"execution_mode: {packet['execution_mode']}")
        if packet.get("suggested_execution_tier"):
            lines.append(f"suggested_execution_tier: {packet['suggested_execution_tier']}")
        if packet.get("background_job_type"):
            lines.append(f"background_job_type: {packet['background_job_type']}")
        if packet.get("background_payload"):
            lines.append(f"background_payload: {_format_handoff_background_payload(packet['background_payload'])}")
        if packet.get("background_job_status"):
            lines.append(f"background_job_status: {packet['background_job_status']}")
        if packet.get("dispatched_job_id"):
            lines.append(f"dispatched_job_id: {packet['dispatched_job_id']}")
        if packet.get("model_hint"):
            lines.append(f"model_hint: {packet['model_hint']}")
        if packet.get("executor_used"):
            lines.append(f"executor_used: {packet['executor_used']}")
        if packet.get("model_used"):
            lines.append(f"model_used: {packet['model_used']}")
        if packet.get("definition_of_done"):
            lines.append(f"definition_of_done: {packet['definition_of_done']}")
        if packet.get("expected_output_shape"):
            lines.append(f"expected_output_shape: {packet['expected_output_shape']}")
        if packet.get("phase_objective"):
            lines.append(f"phase_objective: {packet['phase_objective']}")
        if packet.get("pickup_instruction"):
            lines.append(f"Instruction: {packet['pickup_instruction']}")
    return "\n".join(lines)


def _format_route_task_packet_execution(data: dict[str, Any]) -> str:
    def _compact_value(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()[:180]
        if isinstance(value, dict):
            keys = (
                "name",
                "id",
                "component",
                "component_id",
                "executor",
                "model_id",
                "executor_used",
                "model_used",
                "tier",
                "score",
                "confidence",
                "reason",
                "summary",
                "description",
            )
            parts: list[str] = []
            for key in keys:
                raw = value.get(key)
                if raw not in (None, "", [], {}):
                    parts.append(f"{key}={raw}")
                if len(parts) >= 4:
                    break
            if not parts:
                for key, raw in value.items():
                    if raw not in (None, "", [], {}):
                        parts.append(f"{key}={raw}")
                    if len(parts) >= 4:
                        break
            return "; ".join(parts)[:180]
        if isinstance(value, list):
            items = [_compact_value(item) for item in value if str(item).strip()]
            return ", ".join(item for item in items if item)[:180]
        return str(value).strip()[:180]

    lines = ["Route execution recommendation:"]
    if data.get("memory_id"):
        lines.append(f"memory_id: {data['memory_id']}")
    packet = data.get("packet") or {}
    if packet:
        summary_parts: list[str] = []
        if packet.get("task_description"):
            summary_parts.append(f"task_description={packet['task_description'][:120]}")
        if packet.get("phase"):
            summary_parts.append(f"phase={packet['phase']}")
        if packet.get("execution_mode"):
            summary_parts.append(f"execution_mode={packet['execution_mode']}")
        if packet.get("suggested_execution_tier"):
            summary_parts.append(f"suggested_execution_tier={packet['suggested_execution_tier']}")
        if packet.get("background_job_type"):
            summary_parts.append(f"background_job_type={packet['background_job_type']}")
        if packet.get("background_payload"):
            summary_parts.append(f"background_payload={_format_handoff_background_payload(packet['background_payload'])}")
        if packet.get("background_job_status"):
            summary_parts.append(f"background_job_status={packet['background_job_status']}")
        if packet.get("dispatched_job_id"):
            summary_parts.append(f"dispatched_job_id={packet['dispatched_job_id']}")
        if packet.get("model_hint"):
            summary_parts.append(f"model_hint={packet['model_hint']}")
        if packet.get("priority"):
            summary_parts.append(f"priority={packet['priority']}")
        if packet.get("owner_agent"):
            summary_parts.append(f"owner_agent={packet['owner_agent']}")
        if packet.get("write_scope"):
            summary_parts.append(f"write_scope={_format_handoff_scope(packet['write_scope'])}")
        if packet.get("executor_used"):
            summary_parts.append(f"executor_used={packet['executor_used']}")
        if packet.get("model_used"):
            summary_parts.append(f"model_used={packet['model_used']}")
        if summary_parts:
            lines.append("packet: " + " ".join(summary_parts))
        if packet.get("definition_of_done"):
            lines.append(f"definition_of_done: {packet['definition_of_done']}")
        if packet.get("expected_output_shape"):
            lines.append(f"expected_output_shape: {packet['expected_output_shape']}")
    if data.get("packet_profile") is not None:
        lines.append(f"packet_profile: {_compact_value(data['packet_profile'])}")
    if data.get("routing_basis") is not None:
        lines.append(f"routing_basis: {_compact_value(data['routing_basis'])}")
    if data.get("eligible_executors") is not None:
        lines.append(f"eligible_executors: {_compact_value(data['eligible_executors'])}")
    if data.get("recommended_executor") is not None:
        lines.append(f"recommended_executor: {_compact_value(data['recommended_executor'])}")
    if data.get("recommended_model") is not None:
        lines.append(f"recommended_model: {_compact_value(data['recommended_model'])}")
    if data.get("recommendation_reason"):
        lines.append(f"recommendation_reason: {data['recommendation_reason']}")
    return "\n".join(lines)


def _format_dispatch_background_task_packet(data: dict[str, Any]) -> str:
    lines = ["Background dispatch queued:"]
    if data.get("memory_id"):
        lines.append(f"memory_id: {data['memory_id']}")
    if data.get("status"):
        lines.append(f"status: {data['status']}")
    if data.get("executor_used"):
        lines.append(f"executor_used: {data['executor_used']}")
    if data.get("model_used"):
        lines.append(f"model_used: {data['model_used']}")
    if data.get("background_job_type"):
        lines.append(f"background_job_type: {data['background_job_type']}")
    if data.get("job_id"):
        lines.append(f"job_id: {data['job_id']}")
    if data.get("background_job_status"):
        lines.append(f"background_job_status: {data['background_job_status']}")
    if data.get("dispatched_job_id"):
        lines.append(f"dispatched_job_id: {data['dispatched_job_id']}")
    if data.get("poll"):
        lines.append(f"poll: {data['poll']}")
    if data.get("recommendation_reason"):
        lines.append(f"recommendation_reason: {data['recommendation_reason']}")
    return "\n".join(lines)


def _format_reconcile_background_task_packet(data: dict[str, Any]) -> str:
    lines = ["Background job reconciled:"]
    if data.get("memory_id"):
        lines.append(f"memory_id: {data['memory_id']}")
    if data.get("status"):
        lines.append(f"status: {data['status']}")
    if data.get("job_id"):
        lines.append(f"job_id: {data['job_id']}")
    if data.get("background_job_status"):
        lines.append(f"background_job_status: {data['background_job_status']}")
    if data.get("background_job_type"):
        lines.append(f"background_job_type: {data['background_job_type']}")
    if data.get("executor_used"):
        lines.append(f"executor_used: {data['executor_used']}")
    if data.get("model_used"):
        lines.append(f"model_used: {data['model_used']}")
    if data.get("result_summary"):
        lines.append(f"result_summary: {data['result_summary']}")
    if data.get("verification_summary"):
        lines.append(f"verification_summary: {data['verification_summary']}")
    if data.get("poll"):
        lines.append(f"poll: {data['poll']}")
    return "\n".join(lines)
