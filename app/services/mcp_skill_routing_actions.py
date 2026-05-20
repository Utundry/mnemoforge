from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


GetCallback = Callable[[str, str], Awaitable[Any]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class SkillRoutingActionDependencies:
    get: GetCallback
    post: PostCallback


SKILL_ROUTING_ACTIONS = {
    "crystallize_solution",
    "draft_skill",
    "route_task",
    "track_task",
    "tracker_stats",
    "skill_search",
    "skill_publish",
    "skill_install",
}


async def execute_skill_routing_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: SkillRoutingActionDependencies,
) -> str:
    if name == "crystallize_solution":
        data = await dependencies.post(api_base, "/crystallizer/crystallize", args)
        score = float(data.get("reusability_score") or 0.0)
        if data.get("crystallized"):
            return (
                f"Skill crystallized: '{data['skill_name']}'\n"
                f"Score: {score:.2f} | id: {data['skill_id']}\n"
                f"Reason: {data['reason']}\n"
                "Next time this task can be routed to the skill tier."
            )
        return (
            f"Not crystallized (score {score:.2f} < threshold)\n"
            f"Reason: {data['reason']}"
        )

    if name == "draft_skill":
        data = await dependencies.post(api_base, "/crystallizer/draft", args)
        score = float(data.get("reusability_score") or 0.0)
        if not data.get("draft_ready"):
            return (
                f"Draft not generated (score {score:.2f} < threshold)\n"
                f"Reason: {data['reason']}"
            )
        publish_hint = (
            "High score; recommended to publish as-is via skill_publish."
            if data.get("auto_publish_recommended")
            else "Review the draft and edit if needed, then call skill_publish."
        )
        return (
            f"Skill draft ready: '{data['skill_name']}'\n"
            f"Score: {score:.2f} | {publish_hint}\n"
            f"Reason: {data['reason']}\n\n"
            f"--- SKILL.md draft ---\n{data['skill_content']}\n--- end draft ---\n\n"
            f"Call skill_publish(name='{data['skill_name']}', content=<above or edited>, "
            f"platform='{data.get('platform', 'claude')}') to publish."
        )

    if name == "route_task":
        data = await dependencies.post(api_base, "/router/decide", args)
        alts = ", ".join(f"{item['component']}({item['score']:.2f})" for item in data.get("alternatives", []))
        fallbacks = data.get("cloud_fallbacks", [])
        extra_str = ""
        if fallbacks and data["tier"] == "cloud":
            extra_str = "\nCloud fallbacks: " + ", ".join(f"{item['model_id']}({item['score']:.2f})" for item in fallbacks)
        references = data.get("references", [])
        if references and data["tier"] == "reference":
            ref_lines = "\n".join(
                f"  - {item['name']}: {item.get('description','')[:80]}"
                + (f" -> {item['reference_url']}" if item.get("reference_url") else "")
                for item in references
            )
            extra_str = f"\nReferences (pinned resources):\n{ref_lines}"
        return (
            f"Route to: {data['component']} (tier={data['tier']}, score={data['score']:.2f})\n"
            f"Task type: {data['task_type']}\n"
            f"Reasoning: {data['reasoning']}\n"
            f"Alternatives: {alts or 'none'}"
            f"{extra_str}"
        )

    if name == "track_task":
        data = await dependencies.post(api_base, "/tracker/record", args)
        status = "success" if args.get("success") else "failure"
        note = f" -> corrected to '{data['corrected_task_type']}'" if data.get("corrected_task_type") else ""
        return f"{status} Tracked {data['component']} / {data['task_type']}{note} (event #{data['event_id']})"

    if name == "tracker_stats":
        params = []
        if args.get("component"):
            params.append(f"component={args['component']}")
        if args.get("task_type"):
            params.append(f"task_type={args['task_type']}")
        if args.get("since_hours"):
            params.append(f"since_hours={args['since_hours']}")
        qs = "?" + "&".join(params) if params else ""
        rows = await dependencies.get(api_base, f"/tracker/stats{qs}")
        if not rows:
            return "No performance data yet."
        lines = []
        for row in rows:
            bar = "#" * int(row["success_rate"] * 10)
            latency = f" {row['avg_latency_ms']:.0f}ms" if row["avg_latency_ms"] else ""
            lines.append(
                f"{row['component']:20s} / {row['task_type']:25s} {bar} {row['success_rate']:.2f} "
                f"({row['success']} success/{row['fail']} fail){latency}"
            )
        return "\n".join(lines)

    if name == "skill_search":
        params = []
        if args.get("context"):
            params.append(f"context={args['context']}")
        if args.get("domains"):
            params.append(f"domains={args['domains']}")
        if args.get("platform"):
            params.append(f"platform={args['platform']}")
        params.append(f"limit={args.get('limit', 10)}")
        params.append(f"min_relevance={args.get('min_relevance', 0.3)}")
        results = await dependencies.get(api_base, f"/skills/search?{'&'.join(params)}")
        if not results:
            return "No matching skills found."
        lines = []
        for index, skill in enumerate(results, 1):
            tags = ", ".join(skill.get("domain_tags", []))
            lines.append(
                f"{index}. [{skill['platform']}] **{skill['name']}** - {skill['description']}\n"
                f"   domains: {tags}\n"
                f"   id: {skill['id']}\n"
                f"   install: {skill['install_path']}"
            )
        return "\n\n".join(lines)

    if name == "skill_publish":
        data = await dependencies.post(api_base, "/skills/publish", args)
        return f"Published skill '{data['name']}'\nDomain tags: {data['domain_tags']}\nid: {data['id']}"

    if name == "skill_install":
        data = await dependencies.get(api_base, f"/skills/{args['skill_id']}/content")
        return f"Skill: {data['name']}\nInstall to: {data['install_path']}\n\n--- SKILL.md ---\n{data['content']}"

    raise ValueError(f"Unsupported skill/routing action: {name}")
