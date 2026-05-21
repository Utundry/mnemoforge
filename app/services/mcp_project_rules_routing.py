from __future__ import annotations

from typing import Any, Callable


CatalogSelector = Callable[
    [str, tuple[dict[str, Any], ...], dict[str, Any] | None],
    tuple[dict[str, Any] | None, list[dict[str, Any]]],
]
CatalogIntentLookup = Callable[[tuple[dict[str, Any], ...], str], dict[str, Any] | None]
BackendRequested = Callable[[dict[str, Any]], str]


PROJECT_RULES_ROUTE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "intent_type": "propose_law",
        "tool": "create_rule_candidate",
        "mutating": True,
        "examples": (
            "propose new law",
            "create proposed project law",
            "this is a rule",
            "save this as a project rule",
            "add architecture rule",
        ),
        "arg_bonus": ("title", "statement"),
        "reason": "New law proposal intent maps to creating a trial rule candidate through project_rules.",
    },
    {
        "intent_type": "list_laws",
        "tool": "list_project_laws",
        "mutating": False,
        "examples": (
            "list active rules",
            "show project laws",
            "check project laws",
            "find active rules",
            "return rule ids",
            "find laws by topic",
        ),
        "bonus_terms": ("law", "laws", "rule", "rules"),
        "reason": "Law listing/checking intent maps to list_project_laws.",
    },
    {
        "intent_type": "inspect_law",
        "tool": "get_project_law",
        "mutating": False,
        "examples": ("get law", "show rule by id", "inspect project law", "retrieve law details"),
        "arg_bonus": ("law_id",),
        "reason": "A law_id plus inspection intent maps to get_project_law.",
    },
    {
        "intent_type": "list_candidates",
        "tool": "list_rule_candidates",
        "mutating": False,
        "examples": ("list rule candidates", "show pending rules", "candidate list", "show candidate rules"),
        "reason": "Candidate listing intent maps to list_rule_candidates.",
    },
    {
        "intent_type": "review_candidates",
        "tool": "get_rule_candidate_review_packet",
        "mutating": False,
        "examples": (
            "review rule candidates",
            "review trial rules",
            "show due trial rules",
            "review packet",
            "why did you forget this rule",
            "why did the agent miss a rule",
        ),
        "reason": "Rule review/forgetfulness intent maps to a read-only review packet before governance mutation.",
    },
    {
        "intent_type": "promote_candidate",
        "tool": "promote_rule_candidate",
        "mutating": True,
        "examples": (
            "promote rule candidate",
            "activate this rule candidate",
            "confirm candidate as law",
            "make candidate active",
        ),
        "arg_bonus": ("candidate_id",),
        "reason": "Candidate promotion intent is mutating and must be explicitly confirmed.",
    },
    {
        "intent_type": "revise_law",
        "tool": "revise_law_from_rule_candidate",
        "mutating": True,
        "examples": ("revise law from candidate", "update law from rule candidate", "create law revision"),
        "arg_bonus": ("candidate_id", "law_id"),
        "reason": "Law revision intent is mutating and must be explicitly confirmed.",
    },
    {
        "intent_type": "review_candidate",
        "tool": "review_rule_candidate",
        "mutating": True,
        "examples": (
            "reject rule candidate",
            "suppress candidate",
            "mark candidate needs clarification",
            "reopen rule candidate",
        ),
        "arg_bonus": ("candidate_id", "action"),
        "reason": "Candidate review changes candidate state and must be explicitly confirmed.",
    },
    {
        "intent_type": "expire_trial_candidates",
        "tool": "expire_trial_rule_candidates",
        "mutating": True,
        "examples": (
            "expire stale trial rules",
            "suppress expired trial rule candidates",
            "clean up old trial candidates",
        ),
        "reason": "Expiring stale trial candidates suppresses candidates and must be explicitly confirmed.",
    },
    {
        "intent_type": "project_candidates_from_stenography",
        "tool": "project_rule_candidates_from_stenography",
        "mutating": True,
        "examples": (
            "extract rule markers",
            "make candidates from stenography",
            "create rule candidates from spans",
            "project rule markers",
        ),
        "reason": "Projecting stenographer markers creates review candidates and is guarded as a mutation.",
    },
)


def project_rules_route(
    args: dict[str, Any],
    *,
    select_catalog_route: CatalogSelector,
    catalog_route_by_intent: CatalogIntentLookup,
    backend_requested: BackendRequested,
    llm_decision: dict[str, Any] | None = None,
    scorer_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = str(args.get("intent") or "").strip()
    structural_route = _structural_route(args)
    selected_catalog_route, route_candidates = select_catalog_route(intent, PROJECT_RULES_ROUTE_CATALOG, args)
    structural_match = False
    if structural_route is not None:
        selected_catalog_route = structural_route
        structural_match = True
    if llm_decision:
        selected_catalog_route = catalog_route_by_intent(
            PROJECT_RULES_ROUTE_CATALOG,
            str(llm_decision.get("intent_type") or ""),
        )
    if selected_catalog_route is None:
        selected_catalog_route = next(item for item in PROJECT_RULES_ROUTE_CATALOG if item["intent_type"] == "review_candidates")
    confidence = float((llm_decision or {}).get("confidence") or selected_catalog_route.get("confidence") or 0.65)
    payload_args = _payload_args_for_route(args, selected_catalog_route, llm_decision)
    payload = _payload_for_intent(payload_args, str(selected_catalog_route["intent_type"]))
    return {
        "tool": selected_catalog_route["tool"],
        "intent_type": selected_catalog_route["intent_type"],
        "mutating": bool(selected_catalog_route.get("mutating")),
        "confidence": min(1.0, max(0.0, confidence)),
        "reason": str((llm_decision or {}).get("reason") or selected_catalog_route.get("reason") or "").strip(),
        "matched_example": str((llm_decision or {}).get("matched_example") or selected_catalog_route.get("matched_example") or "").strip(),
        "route_candidates": route_candidates,
        "structural_match": structural_match,
        "scorer": scorer_meta or {
            "backend_requested": backend_requested(args),
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
        "payload": {key: value for key, value in payload.items() if value not in (None, "")},
    }


def _payload_args_for_route(
    args: dict[str, Any],
    selected_catalog_route: dict[str, Any],
    llm_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    if (
        selected_catalog_route["intent_type"] == "list_laws"
        and llm_decision
        and not str(args.get("status") or "").strip()
    ):
        llm_text = " ".join(
            [
                str(llm_decision.get("reason") or ""),
                str(llm_decision.get("matched_example") or ""),
            ]
        ).casefold()
        if "active" in llm_text:
            return {**args, "status": "active"}
    return args


def _structural_route(args: dict[str, Any]) -> dict[str, Any] | None:
    intent = str(args.get("intent") or "").casefold()
    has_law_shape = bool(str(args.get("title") or "").strip() and str(args.get("statement") or "").strip())
    proposal_terms = ("propose", "proposal", "new law", "create law", "create rule", "this is a rule", "save this as")
    if has_law_shape and any(term in intent for term in proposal_terms):
        return next(item for item in PROJECT_RULES_ROUTE_CATALOG if item["intent_type"] == "propose_law")
    return None


def _payload_for_intent(args: dict[str, Any], intent_type: str) -> dict[str, Any]:
    intent = str(args.get("intent") or "").strip()
    lowered_intent = intent.casefold()
    project = str(args.get("project") or "mnemoforge").strip() or "mnemoforge"
    candidate_id = str(args.get("candidate_id") or "").strip()
    law_id = str(args.get("law_id") or "").strip()
    limit = max(1, min(500, int(args.get("limit") or 100)))
    max_matches = max(0, min(20, int(args.get("max_matches") or 5)))
    source_task_id = str(args.get("source_task_id") or "").strip()
    status = str(args.get("status") or "").strip()
    review_due_requested = bool(args.get("review_due", False)) or (
        "due" in lowered_intent and ("trial" in lowered_intent or "review" in lowered_intent)
    )
    if intent_type == "inspect_law":
        return {"law_id": law_id}
    if intent_type == "list_laws":
        valid_law_statuses = {
            "observed", "proposed", "reviewed", "user_confirmed", "active",
            "suppressed", "superseded", "archived", "all",
        }
        selected_status = status if status in valid_law_statuses else ("active" if "active" in lowered_intent else "all")
        return {
            "project": project,
            "status": selected_status,
            "include_promoted": True,
            "query": intent,
            "limit": min(limit, 200),
        }
    if intent_type == "propose_law":
        return {
            "project": project,
            "title": args.get("title"),
            "statement": args.get("statement") or intent,
            "rationale": args.get("rationale") or "",
            "evidence_refs": args.get("evidence_refs") or args.get("evidence") or [],
            "target_scope": args.get("target_scope") or "project",
            "target_status": args.get("target_status") or "trial",
            "confirmed_by": args.get("confirmed_by"),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "topic_path": args.get("topic_path"),
            "promotion_hint": args.get("promotion_hint") or "Review this trial rule after practical use.",
            "review_after_days": args.get("review_after_days", 7),
            "trial_days": args.get("trial_days", 30),
        }
    if intent_type == "promote_candidate":
        return {
            "candidate_id": candidate_id,
            "title": args.get("title"),
            "target_scope": args.get("target_scope"),
            "status": args.get("target_status", "proposed"),
            "reason": str(args.get("reason") or intent).strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "confirmed_by": args.get("confirmed_by"),
        }
    if intent_type == "revise_law":
        return {
            "candidate_id": candidate_id,
            "law_id": law_id,
            "reason": str(args.get("reason") or intent).strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
            "title": args.get("title"),
            "statement": args.get("statement"),
            "rationale": args.get("rationale"),
            "evidence": args.get("evidence") or [],
        }
    if intent_type == "review_candidate":
        lowered = intent.casefold()
        action = str(args.get("action") or "").strip()
        if not action:
            action = (
                "needs_clarification" if "clarification" in lowered
                else "reopen" if "reopen" in lowered
                else "suppress" if "suppress" in lowered
                else "reject"
            )
        return {
            "candidate_id": candidate_id,
            "action": action,
            "reason": str(args.get("reason") or intent).strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
        }
    if intent_type == "project_candidates_from_stenography":
        return {"project": project, "limit": limit}
    if intent_type == "expire_trial_candidates":
        return {
            "project": project,
            "limit": limit,
            "reason": str(args.get("reason") or "Trial rule candidate expired without enough evidence.").strip(),
            "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
        }
    status_options = {"candidate", "needs_clarification", "trial", "revision_pending", "rejected", "suppressed"}
    candidate_payload = {
        "project": project,
        "status": status if status in status_options else "trial" if review_due_requested else "candidate",
        "source_task_id": source_task_id,
        "review_due": review_due_requested,
        "limit": limit,
    }
    if intent_type != "list_candidates":
        candidate_payload["max_matches"] = max_matches
    return candidate_payload
