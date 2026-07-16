"""Onboarding and outcome MCP tool actions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.operational_instincts_service import get_active_operational_instincts, render_onboarding_instincts_block

GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
ErrorFormatter = Callable[[Exception], str]


@dataclass(frozen=True)
class OnboardingActionDependencies:
    get: GetCallback
    post: PostCallback
    format_error: ErrorFormatter


ONBOARDING_ACTIONS = {"get_onboarding", "record_outcome"}


async def execute_onboarding_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    session_id: str | None,
    dependencies: OnboardingActionDependencies,
) -> str:
    if name == "get_onboarding":
        return await _get_onboarding(args=args, api_base=api_base, session_id=session_id, dependencies=dependencies)
    if name == "record_outcome":
        data = await dependencies.post(api_base, "/skills/outcome", {
            "pack_id": args.get("pack_id", "manual"),
            "agent_id": args.get("agent_id", "default"),
            "skills_helpful": args.get("skills_helpful", []),
            "skills_unused": args.get("skills_unused", []),
            "missing_domains": args.get("missing_domains", []),
            "success": args.get("success", True),
        })
        return (
            "Outcome recorded. Thank you - this improves onboarding for future agents.\n"
            f"report_id={data.get('report_id', '?')} success={data.get('stats', {}).get('success')}"
        )
    raise ValueError(f"Unsupported onboarding action: {name}")


async def _get_onboarding(
    *,
    args: dict[str, Any],
    api_base: str,
    session_id: str | None,
    dependencies: OnboardingActionDependencies,
) -> str:
    from app.services.instruction_layers import build_layered_onboarding

    agent_id = args.get("agent_id", "default")
    task_desc = args.get("task_description", "")

    tools_called = []
    if session_id:
        from app.services.mcp_session_store import get_session_store

        ctx = await get_session_store().get_context(session_id)
        if ctx:
            tools_called = [item["tool"] for item in ctx.get("tools_called", [])]

    domains: list[str] = []
    inferred = False
    top_domain = "general"
    confidence = 0.0
    signals: list[str] = []
    try:
        if task_desc:
            profile = await dependencies.post(api_base, "/skills/profile", {"text": task_desc})
            domains = profile.get("domains", []) or []
        else:
            session_hints = []
            if session_id:
                from app.services.mcp_session_store import get_session_store

                ctx = await get_session_store().get_context(session_id)
                if ctx:
                    session_hints = [item["tool"] for item in ctx.get("tools_called", [])]
            inference = await dependencies.post(api_base, "/skills/infer-domain", {
                "agent_id": agent_id,
                "session_hints": session_hints,
            })
            domains = inference.get("all_domains", []) or []
            top_domain = inference.get("domain", "general")
            confidence = inference.get("confidence", 0.0)
            signals = inference.get("signals", [])
            inferred = True
    except Exception:
        domains = ["general"]

    if not domains:
        domains = ["general"]

    layered_content = build_layered_onboarding(
        task_description=task_desc,
        priority="normal",
        phase="",
        next_steps=None,
        tools_called=tools_called,
        domains=domains,
        include_l2=True,
    )

    sections: list[str] = [layered_content]
    pack_id = ""

    if inferred and top_domain != "general":
        sections.append(
            "ORIENTATION (inferred from your history):\n"
            f"  You appear to be working in: {top_domain} (confidence: {confidence:.0%})\n"
            f"  Evidence: {'; '.join(signals[:3])}"
        )
    elif inferred:
        sections.append(
            "ORIENTATION: No prior history found - you are a new agent.\n"
            "  Call record_outcome after your session to help future agents."
        )

    trust_status = "ok"
    try:
        storage_trust = await dependencies.get(api_base, "/admin/storage-trust")
        trust_status = storage_trust.get("status", "unknown")
        if trust_status != "ok":
            trust_summary = storage_trust.get("summary") or "Storage trust is not fully healthy."
            sections.append(
                "STORAGE TRUST WARNING:\n"
                f"  Status: {trust_status}\n"
                f"  {trust_summary}\n"
                "  Call get_storage_trust_status for current degraded slices, hygiene findings, and next actions."
            )
    except Exception:
        pass

    sections.append(
        "EXPERT HELPER GUIDANCE:\n"
        "  Public surface first: help, state, get, submit.\n"
        "  Do not bootstrap from mcp_settings.json, alwaysAllow, client allowlists, or cached full tool lists.\n"
        "  Start project work with state for the current public workflow packet.\n"
        "  Use get for public refs/read-only questions and submit for public forms before falling back to specialized tools.\n"
        "  Use ask_project/project_work only when state/get/help directs a facade fallback or for natural human/project questions.\n"
        "  Stay on the compact surface unless you need deep/debug access.\n"
        "  For task continuation, use get with task:<project>:<task_id> or submit get_task_context first; use reopen_task only to reactivate a closed/inactive task.\n"
        "  Treat runtime details such as Docker test contours as project-specific hints from project context, not universal rules."
    )

    sections.append(
        render_onboarding_instincts_block(
            get_active_operational_instincts(
                context_type="onboarding",
                storage_trust_status=trust_status,
                limit=8,
            )
        )
    )

    try:
        pinned = await dependencies.get(api_base, "/skills/pinned")
        if isinstance(pinned, list) and pinned:
            ref_skills = [skill for skill in pinned if skill.get("reference_url")]
            plain_pinned = [skill for skill in pinned if not skill.get("reference_url")]
            if ref_skills:
                ref_lines = "\n".join(
                    f"  - {skill['name']}: {skill.get('description','')[:80]} -> {skill['reference_url']}"
                    for skill in ref_skills
                )
                sections.append(f"EMERGENCY REFERENCES (always available):\n{ref_lines}")
            if plain_pinned:
                skill_lines = "\n".join(f"  - {skill['name']}: {skill.get('description','')[:80]}" for skill in plain_pinned)
                sections.append(f"PINNED SKILLS (always relevant):\n{skill_lines}")
    except Exception:
        pass

    try:
        from app.services.behavior_adaptation import iter_behavior_evals

        automatable = [
            pattern for pattern in iter_behavior_evals(agent_id)
            if pattern.suggest_automation and not pattern.high_risk
        ]
        if automatable:
            lines = []
            for pattern in automatable[:5]:
                ctx = f" ({pattern.context_signature})" if pattern.context_signature else ""
                lines.append(
                    f"  - {pattern.action_type}{ctx}: confidence={pattern.confidence} accepts={pattern.accepts}"
                )
            sections.append(
                "AUTOMATABLE HABITS (conditional reflexes):\n"
                + "\n".join(lines)
                + "\n\n"
                "TIP: You may start doing these automatically. If the user opts out, "
                "call POST /skills/behavior/patterns/{action_type}/reset."
            )
    except Exception:
        pass

    try:
        pack = await dependencies.post(api_base, "/skills/pack/create", {
            "domains": domains,
            "task_type": "onboarding",
            "agent_id": agent_id,
            "confidence": 0.6,
            "limit": 5,
        })
        pack_id = pack.get("pack_id", "")
        skills = pack.get("skills", [])
        if session_id:
            from app.services.mcp_session_store import get_session_store

            await get_session_store().patch_context(session_id, {
                "pack_id": pack_id,
                "skills_received": [skill.get("id", "") for skill in skills],
            })

        label = "SKILLS FOR YOUR SESSION" if not inferred else f"SKILLS FOR DOMAIN '{domains[0]}'"
        if pack.get("degraded"):
            reason = pack.get("degraded_reason") or "Skill retrieval is running in degraded mode."
            sections.append(f"INTEGRITY WARNING: {reason}")
        if skills:
            skill_lines = "\n".join(f"  - {skill['name']}: {skill.get('description','')[:80]}" for skill in skills)
            sections.append(f"{label} (pack_id={pack_id}):\n{skill_lines}")
        else:
            sections.append("No specific skills found yet - contribute outcomes to improve this.")
    except Exception as exc:
        reason = dependencies.format_error(exc)
        sections.append(
            f"Skills: temporarily unavailable ({reason}). "
            "Continue with onboarding basics and retry later."
        )

    try:
        gaps_data = await dependencies.get(api_base, f"/skills/gaps?agent_id={agent_id}&min_count=1")
        gaps = gaps_data.get("gaps", [])
        if gaps:
            gap_lines = ", ".join(gap["domain"] for gap in gaps[:5])
            sections.append(f"KNOWN KNOWLEDGE GAPS (from past sessions): {gap_lines}")
    except Exception:
        pass

    try:
        analytics = await dependencies.get(api_base, f"/skills/analytics?agent_id={agent_id}")
        total = analytics.get("total_outcomes", 0)
        rate = analytics.get("success_rate")
        if total > 0:
            sections.append(
                f"COLLECTIVE EXPERIENCE: {total} past sessions, "
                f"{int((rate or 0)*100)}% success rate"
            )
    except Exception:
        pass

    try:
        recent = await dependencies.get(api_base, f"/memories/recent?agent_id={agent_id}&limit=3&minutes=10080")
        if isinstance(recent, list) and recent:
            mem_lines = "\n".join(f"  - {memory['content'][:100]}" for memory in recent[:3])
            sections.append(f"RECENT CONTEXT FOR YOUR AGENT:\n{mem_lines}")
    except Exception:
        pass

    sections.append(
        "TIP: Call record_outcome at the end of your session to teach the system. "
        f"Use pack_id={pack_id!r} to reference this session's skill pack."
    )
    return "\n\n".join(sections)
