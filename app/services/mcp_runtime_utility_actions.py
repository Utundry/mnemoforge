from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.mcp_tool_contracts import (
    build_list_instruction_layers_payload,
    format_list_instruction_layers_response,
    format_load_instruction_layer_response,
)


GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class RuntimeUtilityActionDependencies:
    get: GetCallback
    post: PostCallback


async def execute_runtime_utility_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: RuntimeUtilityActionDependencies,
) -> str:
    if name == "memory_health":
        data = await dependencies.get(api_base, "/health")
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "system_info":
        data = await dependencies.get(api_base, "/system/info")
        infra = data.get("infrastructure", {})
        counters = data.get("counters", {})
        components = data.get("components", [])
        llm_providers = infra.get("llm_providers", {}) if isinstance(infra.get("llm_providers"), dict) else {}
        available_llms = llm_providers.get("available_llms") if isinstance(llm_providers.get("available_llms"), list) else []
        usable_providers = llm_providers.get("usable_providers") if isinstance(llm_providers.get("usable_providers"), list) else []
        provider_matrix = llm_providers.get("providers") if isinstance(llm_providers.get("providers"), dict) else {}
        qdrant_status = "ok" if infra.get("qdrant", {}).get("reachable") else "fail"
        llm_status = "ok" if llm_providers.get("healthy") else "fail"
        provider_summary = ", ".join(str(item) for item in usable_providers) or "none"
        model_summary = ", ".join(
            str(item.get("id") or item.get("provider") or "llm")
            for item in available_llms
            if isinstance(item, dict)
        ) or "none"
        if model_summary == "none":
            ollama_models = infra.get("ollama", {}).get("models", [])
            if ollama_models:
                model_summary = ", ".join(str(item) for item in ollama_models)
        embedding_provider = "gateway"
        for provider_name, provider in provider_matrix.items():
            if not isinstance(provider, dict):
                continue
            if provider.get("enabled") and provider.get("reachable") and provider.get("kind") in {"local", "local_openai_compatible"}:
                embedding_provider = str(provider_name)
                break

        lines = [
            f"SloplessCode status: {data.get('status','?')} | uptime: {data.get('uptime_seconds',0)//60}m",
            f"Qdrant: {qdrant_status}  LLM providers: {llm_status}  usable: {provider_summary}  "
            f"embedding: {embedding_provider}/{infra.get('embedding_model','?')} ({infra.get('embedding_dimensions','?')}d)",
            f"Models: {model_summary}",
            "",
            f"Counters: memories={counters.get('memories',0)}  "
            f"skills={counters.get('skills',0)}  "
            f"layout_terms={counters.get('layout_terms',0)}",
            "",
            f"Components ({len(components)}):",
        ]
        for component in components:
            tag = "[core]" if component.get("status") == "core" else "[opt] "
            lines.append(f"  {tag} {component['id']:20s} - {component['description'][:80]}")
            endpoints = component.get("endpoints") or []
            if endpoints:
                lines.append(f"    endpoints: {', '.join(endpoints)}")
        return "\n".join(lines)

    if name == "memory_stats":
        data = await dependencies.get(api_base, "/memories/stats")
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "registry_best":
        params = f"task_type={args['task_type']}&top={args.get('top', 3)}"
        if args.get("exclude"):
            params += f"&exclude={args['exclude']}"
        data = await dependencies.get(api_base, f"/registry/best?{params}")
        lines = [f"Best components for '{data['task_type']}':"]
        for index, result in enumerate(data["ranked"], 1):
            filled = int(result["score"] * 10)
            bar = "#" * filled + "." * (10 - filled)
            lines.append(f"  {index}. {result['component']:20s} {bar} {result['score']:.3f}")
        return "\n".join(lines)

    if name == "registry_update":
        data = await dependencies.post(api_base, "/registry/update", args)
        status = "ok" if args.get("success") else "failed"
        return f"{status} Updated {data['component']} / {data['task_type']} -> score: {data['new_score']}"

    if name == "registry_components":
        data = await dependencies.get(api_base, "/registry/components")
        lines = []
        for component, capabilities in data.items():
            lines.append(f"\n{component}:")
            for task, info in sorted(capabilities.items(), key=lambda item: -item[1]["score"]):
                bar = "#" * int(info["score"] * 10)
                lines.append(
                    f"  {task:25s} {bar} {info['score']:.2f}  "
                    f"({info['success']} ok/{info['fail']} fail)"
                )
        return "\n".join(lines)

    if name == "load_instruction_layer":
        from app.services.instruction_layers import get_l3_layer, get_l4_layer

        layer = args.get("layer", "L3")
        if layer == "L3":
            category = args.get("category", "memory_operations")
            section = args.get("section", "api_reference")
            content = get_l3_layer(category, section)
        elif layer == "L4":
            section = args.get("section", "advanced_patterns")
            content = get_l4_layer(section)
        else:
            return f"Invalid layer: {layer}. Use 'L3' or 'L4'."
        return format_load_instruction_layer_response(content)

    if name == "list_instruction_layers":
        from app.services.instruction_layers import list_available_layers

        payload = build_list_instruction_layers_payload(args)
        layers = list_available_layers(payload.get("layer"))
        return format_list_instruction_layers_response(layers)

    if name == "model_available":
        params = ""
        if args.get("task_type"):
            params = f"?task_type={args['task_type']}"
        models = await dependencies.get(api_base, f"/models/available{params}")
        if not models:
            return "No available cloud models. All models may be at quota or in cooldown."
        lines = ["Available cloud models:"]
        for model in models:
            filled = int(model["remaining_pct"] / 10)
            bar = "#" * filled + "." * (10 - filled)
            lines.append(
                f"  {model['priority']}. {model['model_id']:15s} [{model['provider']}] "
                f"{bar} {model['remaining_pct']:.0f}% remaining "
                f"({model['remaining']:,} {model['limit_unit']})"
            )
        return "\n".join(lines)

    if name == "report_limit_hit":
        data = await dependencies.post(api_base, "/models/report_limit", args)
        cooldown = data.get("cooldown_until")
        if cooldown:
            seconds = max(0, int(cooldown - time.time()))
            return f"{args['model_id']} marked as rate-limited. Cooldown: {seconds}s. Use model_available to find alternatives."
        return f"{args['model_id']} marked as rate-limited. Use model_available to find alternatives."

    if name == "get_task_status":
        data = await dependencies.get(api_base, f"/tasks/{args['job_id']}")
        status = data.get("status", "unknown")
        job_type = data.get("job_type", "")
        lines = [f"Job {str(args['job_id'])[:8]}... | type={job_type} | status={status}"]
        if status == "done":
            result = data.get("result") or {}
            lines.append(f"Result: {result}")
        elif status == "failed":
            lines.append(f"Error: {data.get('error', 'unknown error')}")
        elif status == "running":
            started = data.get("started_at")
            lines.append(f"Started at: {started}")
        return "\n".join(lines)

    raise ValueError(f"Unsupported runtime utility action: {name}")
