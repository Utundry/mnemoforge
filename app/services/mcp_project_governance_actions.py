from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote


GetCallback = Callable[[str, str], Awaitable[dict[str, Any]]]
PostCallback = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
AnnotatePayloadCallback = Callable[[str, dict[str, Any]], dict[str, Any]]
RuleRefsCallback = Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass(frozen=True)
class ProjectGovernanceActionDependencies:
    get: GetCallback
    post: PostCallback
    annotate_payload: AnnotatePayloadCallback
    project_context_rule_refs: RuleRefsCallback


async def execute_project_governance_action(
    *,
    name: str,
    args: dict[str, Any],
    api_base: str,
    dependencies: ProjectGovernanceActionDependencies,
) -> str:
    if name == "list_project_aliases":
        project_id = str(args.get("project_id") or "").strip()
        suffix = f"?project_id={quote(project_id, safe='')}" if project_id else ""
        data = await dependencies.get(api_base, f"/project/identity/aliases{suffix}")
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "rename_project":
        payload = {
            "old_project_id": args["old_project_id"],
            "new_project_id": args["new_project_id"],
            "apply": bool(args.get("apply", False)),
            "include_text": bool(args.get("include_text", False)),
            "ensure_alias": bool(args.get("ensure_alias", True)),
            "reason": str(args.get("reason") or ""),
        }
        data = await dependencies.post(api_base, "/project/rename", payload)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "list_project_laws":
        status = str(args.get("status") or "all").strip() or "all"
        params = [
            f"status={status}",
            f"limit={int(args.get('limit', 20))}",
            f"include_promoted={str(bool(args.get('include_promoted', True))).lower()}",
        ]
        if args.get("project"):
            params.append(f"project={args['project']}")
        if args.get("scope"):
            params.append(f"scope={args['scope']}")
        data = await dependencies.get(api_base, f"/laws?{'&'.join(params)}")
        items = list(data.get("items", []) or [])
        context_refs = dependencies.project_context_rule_refs(args)
        existing_ids = {str(item.get("id") or "") for item in items if isinstance(item, dict)}
        items.extend(ref for ref in context_refs if str(ref.get("id") or "") not in existing_ids)
        if not items:
            return "No matching project laws."
        lines = []
        for i, item in enumerate(items, 1):
            locality = "project-local" if item.get("is_project_local") else item.get("scope", "?")
            source = str(item.get("source") or "laws").strip()
            source_suffix = f" source={source}" if source != "laws" else ""
            lines.append(
                f"{i}. [{item.get('status','?')}] {item.get('title','')}\n"
                f"   scope={item.get('scope','?')} locality={locality} project={item.get('project') or '-'}{source_suffix}\n"
                f"   id={item.get('id')}"
            )
            rationale = str(item.get("rationale") or "").strip()
            if rationale:
                lines.append(f"   rationale={rationale[:240]}")
        project = args.get("project", "all")
        return f"Project laws ({project}, {status}):\n\n" + "\n\n".join(lines)

    if name == "get_project_law":
        data = await dependencies.get(api_base, f"/laws/{args['law_id']}")
        lines = [
            f"title={data.get('title','')}",
            f"status={data.get('status','?')} scope={data.get('scope','?')} project={data.get('project') or '-'} version={data.get('version','1.0')}",
            f"statement={data.get('statement','')}",
        ]
        if data.get("rationale"):
            lines.append(f"rationale={data['rationale']}")
        evidence = data.get("evidence") or []
        if evidence:
            lines.append("evidence:")
            lines.extend(f"- {item}" for item in evidence[:5])
        candidate = data.get("candidate_revision")
        if candidate:
            lines.append(f"candidate_status={candidate.get('status', 'proposed')}")
            lines.append(f"candidate_statement={candidate.get('statement', '')}")
        if data.get("confirmed_by"):
            lines.append(f"confirmed_by={data.get('confirmed_by')}")
        lines.append(f"id={data.get('id')}")
        return "\n".join(lines)

    if name == "create_project_law":
        payload = {
            "project": str(args.get("project") or "mnemoforge").strip() or "mnemoforge",
            "title": str(args["title"]).strip(),
            "statement": str(args["statement"]).strip(),
            "rationale": str(args.get("rationale") or "").strip(),
            "evidence": args.get("evidence") or [],
            "agent_id": str(args.get("acted_by") or args.get("agent_id") or "codex").strip() or "codex",
            "scope": str(args.get("target_scope") or args.get("scope") or "project").strip() or "project",
            "status": str(args.get("target_status") or args.get("status") or "proposed").strip() or "proposed",
            "confirmed_by": args.get("confirmed_by"),
            "confirmation_source": args.get("confirmation_source", "mcp_project_rules"),
            "tags": args.get("tags") or ["project_rules"],
            "topic_path": args.get("topic_path"),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        data = await dependencies.post(api_base, "/laws", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "create_rule_candidate":
        candidate_status = str(args.get("target_status") or args.get("status") or "trial").strip() or "trial"
        if candidate_status not in {"candidate", "needs_clarification", "trial", "revision_pending", "rejected", "suppressed"}:
            candidate_status = "trial"
        payload = {
            "project": str(args.get("project") or "mnemoforge").strip() or "mnemoforge",
            "title": str(args.get("title") or "").strip(),
            "statement": str(args["statement"]).strip(),
            "rationale": str(args.get("rationale") or "").strip(),
            "evidence_refs": args.get("evidence_refs") or args.get("evidence") or [],
            "scope": "canonical_candidate"
            if str(args.get("target_scope") or args.get("scope") or "project").strip() in {"family", "domain", "principle", "meta"}
            else "project",
            "topic_path": args.get("topic_path") or "",
            "status": candidate_status,
            "confidence": float(args.get("confidence") or 0.75),
            "promotion_hint": str(args.get("promotion_hint") or "Review this trial rule after practical use.").strip(),
            "review_after_days": int(args.get("review_after_days") or 7),
            "trial_days": int(args.get("trial_days") or 30),
            "source_task_id": args.get("source_task_id") or "",
            "source_session_id": args.get("session_id") or "",
            "source_work_id": args.get("work_id") or "",
            "acted_by": str(args.get("acted_by") or args.get("agent_id") or "codex").strip() or "codex",
            "source": str(args.get("source") or "mcp_project_rules").strip() or "mcp_project_rules",
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        data = await dependencies.post(api_base, "/laws/candidates", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "list_rule_candidates":
        params = [f"limit={int(args.get('limit', 100))}"]
        if args.get("project"):
            params.append(f"project={args['project']}")
        if args.get("status"):
            params.append(f"status={args['status']}")
        if args.get("source_task_id"):
            params.append(f"source_task_id={args['source_task_id']}")
        if args.get("review_due"):
            params.append("review_due=true")
        data = await dependencies.get(api_base, f"/laws/candidates?{'&'.join(params)}")
        items = data.get("items", [])
        if not items:
            return "No matching rule candidates."
        lines = []
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. [{item.get('status','?')}] {item.get('statement','')}\n"
                f"   scope={item.get('scope','?')} project={item.get('project','?')} topic={item.get('topic_path') or '-'}\n"
                f"   candidate_id={item.get('candidate_id')} source_span_id={item.get('source_span_id')}"
            )
        return f"Rule candidates ({data.get('total', len(items))}):\n\n" + "\n\n".join(lines)

    if name == "get_rule_candidate_review_packet":
        payload = {
            "project": args.get("project"),
            "status": args.get("status", "candidate"),
            "source_task_id": args.get("source_task_id"),
            "limit": int(args.get("limit") or 100),
            "max_matches": int(args.get("max_matches") or 5),
        }
        if args.get("review_due"):
            payload["review_due"] = True
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        data = await dependencies.post(api_base, "/laws/candidates/review-packet", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "expire_trial_rule_candidates":
        payload = {
            "project": args.get("project"),
            "limit": int(args.get("limit") or 100),
            "reason": str(args.get("reason") or "Trial rule candidate expired without enough evidence.").strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "source": str(args.get("source") or "mcp_project_rules_trial_expiry").strip(),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        data = await dependencies.post(api_base, "/laws/candidates/expire-trials", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "review_rule_candidate":
        candidate_id = str(args["candidate_id"]).strip()
        payload = {
            "action": str(args["action"]).strip(),
            "reason": str(args["reason"]).strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "source": str(args.get("source") or "mcp_rule_candidate_review").strip() or "mcp_rule_candidate_review",
        }
        data = await dependencies.post(api_base, f"/laws/candidates/{candidate_id}/review", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "promote_rule_candidate":
        candidate_id = str(args["candidate_id"]).strip()
        payload = {
            "title": args.get("title"),
            "target_scope": args.get("target_scope"),
            "status": args.get("status", "proposed"),
            "reason": str(args["reason"]).strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "source": str(args.get("source") or "mcp_rule_candidate_promotion").strip() or "mcp_rule_candidate_promotion",
            "confirmed_by": args.get("confirmed_by"),
            "confirmation_source": args.get("confirmation_source", "mcp_rule_candidate_promotion"),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        data = await dependencies.post(api_base, f"/laws/candidates/{candidate_id}/promote", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    if name == "revise_law_from_rule_candidate":
        candidate_id = str(args["candidate_id"]).strip()
        payload = {
            "law_id": str(args["law_id"]).strip(),
            "reason": str(args["reason"]).strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "source": str(args.get("source") or "mcp_rule_candidate_law_revision").strip()
            or "mcp_rule_candidate_law_revision",
            "title": args.get("title"),
            "statement": args.get("statement"),
            "rationale": args.get("rationale"),
            "evidence": args.get("evidence"),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        data = await dependencies.post(api_base, f"/laws/candidates/{candidate_id}/revise-law", payload)
        data = dependencies.annotate_payload(name, data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    raise ValueError(f"Unsupported project governance action: {name}")
