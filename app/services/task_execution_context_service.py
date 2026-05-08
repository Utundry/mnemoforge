from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.models.law import ProjectLawRecord
from app.models.task_execution_context import (
    OperationTray,
    TaskExecutionContextRequest,
    TaskExecutionContextResponse,
    TaskExecutionReadiness,
    TaskExecutionRuleRef,
    TaskExecutionToolSuggestion,
)
from app.services.law_service import CONFIRMED_STATUSES, list_project_laws
from app.services.project_tree_store import get_tree_store


@dataclass(frozen=True)
class StatePolicy:
    tool_suggestions: tuple[TaskExecutionToolSuggestion, ...] = ()
    assistant_tools: tuple[str, ...] = ()
    diagnostic_tools: tuple[str, ...] = ()
    guarded_tools: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()
    recommended_terms: tuple[str, ...] = ()
    risk_controls: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    next_transitions: tuple[str, ...] = ()


_COMMON_REQUIRED_TERMS = ("english internal", "internal text", "agent internal", "language")


_STATE_POLICIES: dict[str, StatePolicy] = {
    "planning": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["enrich_task_with_context", "list_open_tasks", "list_artifacts"],
                reason="Gather compact project context and current artifacts before choosing work.",
            ),
            TaskExecutionToolSuggestion(
                family="tool_discovery",
                tools=["list_tool_families", "tool_recommend"],
                reason="Use staged discovery instead of loading the full catalog.",
            ),
        ),
        diagnostic_tools=("enrich_task_with_context", "list_open_tasks", "list_artifacts"),
        assistant_tools=("tool_recommend",),
        required_terms=_COMMON_REQUIRED_TERMS,
        recommended_terms=("project laws", "context", "workflow"),
        risk_controls=("Do not start implementation before checking active project laws and open artifacts.",),
        expected_outputs=("Task framing, selected state, scoped rules, and next implementation step.",),
        next_transitions=("implementation", "operator_review", "handoff"),
    ),
    "implementation": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["enrich_task_with_context", "record_task_checkpoint"],
                reason="Keep implementation aligned with project context and durable checkpoints.",
            ),
            TaskExecutionToolSuggestion(
                family="tool_discovery",
                tools=["tool_recommend", "tool_family_tools"],
                reason="Load only the tool family needed by the current implementation slice.",
            ),
        ),
        assistant_tools=("record_task_checkpoint",),
        diagnostic_tools=("enrich_task_with_context", "tool_recommend"),
        required_terms=_COMMON_REQUIRED_TERMS,
        recommended_terms=("testing contour", "docker", "checkpoint"),
        risk_controls=("Do not mutate unrelated files or revert user changes.",),
        expected_outputs=("Small scoped code change ready for verification.",),
        next_transitions=("verification", "checkpointing", "handoff"),
    ),
    "verification": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="testing",
                tools=["tool_recommend", "tool_family_tools"],
                reason="Discover the narrow test command or verification tool before running checks.",
            ),
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["record_task_checkpoint", "report_task_checkpoint"],
                reason="Record verification evidence after checks finish.",
            ),
        ),
        assistant_tools=("record_task_checkpoint", "report_task_checkpoint"),
        diagnostic_tools=("tool_recommend", "tool_family_tools"),
        forbidden_patterns=("host pytest when project rules require an isolated test contour",),
        required_terms=_COMMON_REQUIRED_TERMS + ("test contour", "verification contour", "docker test", "host pytest", "test runner"),
        recommended_terms=("dev server restart", "memory-server-dev", "live validation"),
        risk_controls=(
            "Use the project-approved verification contour; if it is unknown, clarify or inspect it before running tests.",
            "Do not run verification through a path that conflicts with active project rules.",
        ),
        expected_outputs=("Focused test result, command used, and remaining risk if any.",),
        next_transitions=("live_validation", "checkpointing", "implementation"),
    ),
    "live_validation": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["reconcile_completed_checkpoints", "review_completed_checkpoint_scopes"],
                reason="Use report-only lifecycle surfaces before mutating live artifacts.",
            ),
            TaskExecutionToolSuggestion(
                family="tool_discovery",
                tools=["tool_recommend", "tool_feedback"],
                reason="Evaluate testing-stage tools after practical use.",
            ),
        ),
        assistant_tools=("tool_feedback",),
        diagnostic_tools=("reconcile_completed_checkpoints", "review_completed_checkpoint_scopes"),
        guarded_tools=("review_completed_checkpoint_scopes",),
        required_terms=_COMMON_REQUIRED_TERMS + ("dev server restart", "memory-server-dev"),
        recommended_terms=(
            "docker restart",
            "runtime owner",
            "stale window",
            "runtime_owner",
            "120 seconds",
            "docker test",
            "pytest",
            "lifecycle",
            "report-only",
        ),
        risk_controls=(
            "Restart memory-server-dev when needed to load server-side changes.",
            "Keep test execution separate from live validation.",
            "Run report-only checks before close=true or other live mutations.",
        ),
        expected_outputs=("Live behavior report, safe mutation summary, and tool feedback if applicable.",),
        next_transitions=("documentation", "checkpointing", "operator_review", "implementation"),
    ),
    "documentation": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["upsert_knowledge_tree_node", "record_task_checkpoint", "report_task_checkpoint"],
                reason="Write architecture and documentation knowledge into structured memory before Markdown projections.",
            ),
            TaskExecutionToolSuggestion(
                family="tool_discovery",
                tools=["tool_recommend", "tool_family_tools"],
                reason="Load projection or documentation tools only when the current slice requires them.",
            ),
        ),
        assistant_tools=("record_task_checkpoint", "report_task_checkpoint"),
        diagnostic_tools=("upsert_knowledge_tree_node", "tool_recommend"),
        required_terms=_COMMON_REQUIRED_TERMS + ("documentation", "knowledge tree", "projection", "source of truth"),
        recommended_terms=("readme", "structured knowledge", "architecture", "recovery"),
        risk_controls=(
            "Treat Markdown as a generated or validated projection; keep the structured knowledge tree as the source of truth.",
            "Record documentation-stage evidence before moving to handoff or new implementation work.",
        ),
        expected_outputs=("Structured knowledge update, projection target, verification note, and checkpoint evidence.",),
        next_transitions=("checkpointing", "operator_review", "planning", "implementation"),
    ),
    "checkpointing": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["clerk_draft_report", "draft_checkpoint_from_spans", "record_task_checkpoint", "report_task_checkpoint"],
                reason="Use the clerk/scribe path to structure stenographer spans or raw notes before persisting completed work, verification, risks, and next step scope.",
            ),
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["record_stenographer_span", "project_rule_candidates_from_stenography", "list_rule_candidates"],
                reason="Capture explicit rule markers and project them into reviewable candidates after closeout.",
            ),
        ),
        assistant_tools=("clerk_draft_report", "draft_checkpoint_from_spans", "record_stenographer_span"),
        diagnostic_tools=("list_rule_candidates",),
        required_terms=_COMMON_REQUIRED_TERMS + ("checkpoint", "next_step", "next_step_scope", "self-improving project laws"),
        recommended_terms=("stenographer", "clerk", "handoff", "rule", "candidate"),
        risk_controls=(
            "Do not leave completion only in chat; persist task outcome in governed memory.",
            "Use explicit rule marker span kinds for candidate rules; do not activate new laws directly from task notes.",
            "Keep rule candidates in English internal text and review them after task closeout.",
        ),
        expected_outputs=("Checkpoint with status, verification, changed files, risks, and scoped next step.",),
        next_transitions=("handoff", "planning", "operator_review"),
    ),
    "handoff": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["clerk_draft_report", "record_task_checkpoint", "resume_handoff", "expand_handoff_refs"],
                reason="Draft a grounded closeout from stenographer spans before creating or consuming compact handoff packets.",
            ),
        ),
        assistant_tools=("clerk_draft_report", "record_task_checkpoint"),
        diagnostic_tools=("resume_handoff", "expand_handoff_refs"),
        required_terms=_COMMON_REQUIRED_TERMS + ("handoff", "resume"),
        recommended_terms=("checkpoint", "context"),
        risk_controls=("Include enough refs for resume without copying full raw history.",),
        expected_outputs=("Compact resume packet with blockers, risks, refs, and next action.",),
        next_transitions=("planning", "implementation", "operator_review"),
    ),
    "operator_review": StatePolicy(
        tool_suggestions=(
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=["list_artifacts", "review_completed_checkpoint_scopes", "reopen_artifact"],
                reason="Support explicit review decisions before lifecycle mutations.",
            ),
            TaskExecutionToolSuggestion(
                family="project_knowledge",
                tools=[
                    "get_rule_candidate_review_packet",
                    "review_rule_candidate",
                    "promote_rule_candidate",
                    "revise_law_from_rule_candidate",
                    "list_rule_candidates",
                    "list_project_laws",
                    "get_project_law",
                ],
                reason="Review pending rule candidates against active laws and sibling candidates before activation or rejection.",
            ),
        ),
        diagnostic_tools=("list_artifacts", "get_rule_candidate_review_packet", "list_rule_candidates", "list_project_laws", "get_project_law"),
        guarded_tools=("review_completed_checkpoint_scopes", "reopen_artifact", "review_rule_candidate", "promote_rule_candidate", "revise_law_from_rule_candidate"),
        required_terms=_COMMON_REQUIRED_TERMS + ("operator review", "review", "laws live in memory"),
        recommended_terms=("lifecycle", "reconcile", "rule", "candidate"),
        risk_controls=(
            "Do not auto-close or auto-activate items that require operator review.",
            "Do not approve duplicate or overlapping rule candidates without consolidation.",
        ),
        expected_outputs=("Reviewed decision, explicit rationale, and next lifecycle action.",),
        next_transitions=("planning", "implementation", "checkpointing"),
    ),
}


def _haystack(law: ProjectLawRecord) -> str:
    parts = [
        law.title,
        law.statement,
        law.rationale,
        law.topic_path or "",
        " ".join(law.tags or []),
        law.scope,
    ]
    return " ".join(parts).casefold()


def _term_matches(text: str, terms: Iterable[str]) -> bool:
    return any(term.casefold() in text for term in terms if term)


def _task_terms(body: TaskExecutionContextRequest) -> tuple[str, ...]:
    raw = " ".join([body.task, body.intent, " ".join(body.changed_files)]).casefold()
    terms = {
        token.strip(".,:;()[]{}")
        for token in raw.split()
        if len(token.strip(".,:;()[]{}")) >= 5
    }
    return tuple(sorted(terms))


def _rule_ref(law: ProjectLawRecord, *, reason: str) -> TaskExecutionRuleRef:
    return TaskExecutionRuleRef(
        id=law.id,
        title=law.title,
        scope=law.scope,
        status=law.status,
        topic_path=law.topic_path,
        rationale=law.rationale,
        reason=reason,
    )


def _has_recorded_stage_evidence(body: TaskExecutionContextRequest) -> bool:
    if body.prior_stage_recorded is not None:
        return body.prior_stage_recorded
    return bool(body.stage_evidence)


def _looks_like_verification_contour(law: ProjectLawRecord) -> bool:
    text = _haystack(law)
    return any(
        term in text
        for term in (
            "test contour",
            "verification contour",
            "docker test",
            "host pytest",
            "test runner",
            "testing-contour",
        )
    )


def _verification_contour_rules(laws: list[ProjectLawRecord]) -> list[ProjectLawRecord]:
    return [law for law in laws if _looks_like_verification_contour(law)]


def _looks_like_restart_stale_window(law: ProjectLawRecord) -> bool:
    text = _haystack(law)
    return any(
        term in text
        for term in (
            "runtime owner",
            "runtimeownershiperror",
            "runtime_owner",
            "stale window",
            "120 seconds",
            "mnemoforge_runtime_owner_stale_seconds",
        )
    )


def _project_testing_rule_refs(project: str) -> list[TaskExecutionRuleRef]:
    if not project:
        return []
    try:
        nodes = get_tree_store().list_nodes(
            status="active",
            topic_prefix=f"{project}/testing",
            limit=50,
        )
    except Exception:
        return []

    refs: list[TaskExecutionRuleRef] = []
    for node in nodes:
        meta = node.get("meta_json") or {}
        structured = meta.get("structured_knowledge") or {}
        if structured.get("rule_kind") != "project_local_testing_rule":
            continue
        if str(structured.get("applies_to_project") or project) != project:
            continue
        refs.append(
            TaskExecutionRuleRef(
                id=str(node.get("id") or ""),
                title=str(node.get("title") or "Project-local testing rule"),
                scope="project",
                status=str(node.get("status") or "active"),
                topic_path=str(node.get("topic_path") or ""),
                rationale=str(structured.get("responsibility") or node.get("goal") or node.get("description") or ""),
                reason="Matched project-local testing rule from the knowledge tree.",
            )
        )
    return refs


def _build_readiness(
    body: TaskExecutionContextRequest,
    *,
    laws: list[ProjectLawRecord],
    project_testing_rules: list[TaskExecutionRuleRef],
) -> TaskExecutionReadiness:
    missing: list[str] = []
    required: list[str] = []
    evidence = list(body.stage_evidence)

    if body.prior_stage_recorded:
        evidence.append("prior_stage_recorded:true")
    if body.task_id:
        evidence.append(f"task_id:{body.task_id}")

    if body.state == "implementation" and not _has_recorded_stage_evidence(body):
        missing.append("task_framing_not_recorded")
        required.append("Record the finalized task framing in project memory before implementation.")

    if body.state == "verification":
        if not body.changed_files and not _has_recorded_stage_evidence(body):
            missing.append("implementation_evidence_missing")
            required.append("Record or provide implementation evidence before verification.")
        if not _verification_contour_rules(laws) and not project_testing_rules:
            missing.append("verification_contour_unknown")
            required.append("Identify the project-approved verification contour before running tests.")

    if body.state == "live_validation" and not _has_recorded_stage_evidence(body):
        missing.append("verification_evidence_missing")
        required.append("Record verification evidence before live validation.")

    if body.state == "handoff" and not _has_recorded_stage_evidence(body):
        missing.append("handoff_checkpoint_missing")
        required.append("Record current status, blockers, risks, and next step before handoff.")

    reason = "All prerequisites for the requested state are present."
    if missing:
        reason = "The requested state is gated until missing prerequisites are recorded or clarified."

    return TaskExecutionReadiness(
        ready_to_enter=not missing,
        missing_prerequisites=missing,
        required_before_entering=required,
        evidence=evidence,
        reason=reason,
    )


def _flatten_tools(suggestions: list[TaskExecutionToolSuggestion]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for suggestion in suggestions:
        for tool in suggestion.tools:
            if tool not in seen:
                seen.add(tool)
                ordered.append(tool)
    return ordered


def _build_operation_tray(
    body: TaskExecutionContextRequest,
    policy: StatePolicy,
    tools: list[TaskExecutionToolSuggestion],
    readiness: TaskExecutionReadiness,
    risk_controls: list[str],
) -> OperationTray:
    reasons = {tool: suggestion.reason for suggestion in tools for tool in suggestion.tools}
    primary_tools = _flatten_tools(tools)[:8]
    assistant_tools = list(policy.assistant_tools)
    diagnostic_tools = list(policy.diagnostic_tools)
    guarded_tools = list(policy.guarded_tools)

    if not readiness.ready_to_enter:
        primary_tools = ["record_task_checkpoint"]
        assistant_tools = ["clerk_draft_report", "draft_task_checkpoint", *assistant_tools]
        reasons["record_task_checkpoint"] = "Readiness gate requires durable project-memory evidence before entering this state."
        reasons["clerk_draft_report"] = "Use the clerk/scribe path to prepare a review-only draft from stenographer spans or raw notes before memory mutation."
        reasons["draft_task_checkpoint"] = "Use the clerk/scribe path to prepare the missing stage record from raw notes."

    required_records_by_state = {
        "implementation": ["framing_checkpoint"],
        "verification": ["implementation_checkpoint", "verification_contour"],
        "live_validation": ["verification_checkpoint"],
        "documentation": ["structured_knowledge_update"],
        "handoff": ["handoff_checkpoint"],
        "checkpointing": ["task_checkpoint"],
        "operator_review": ["review_decision"],
    }
    avoid_by_state = {
        "implementation": ["full_closeout_report"],
        "verification": ["full_handoff_report"],
        "documentation": ["manual_markdown_as_source_of_truth"],
        "checkpointing": [],
        "operator_review": ["automatic_law_activation_without_review_packet"],
    }
    bureaucracy_mode = "lightweight" if body.state in {"implementation", "verification", "live_validation", "documentation"} else "standard"
    if body.state in {"handoff", "checkpointing"}:
        bureaucracy_mode = "full_when_closing"

    return OperationTray(
        state=body.state,
        primary_tools=primary_tools,
        assistant_tools=list(dict.fromkeys(assistant_tools))[:8],
        diagnostic_tools=list(dict.fromkeys(diagnostic_tools))[:8],
        guarded_tools=list(dict.fromkeys(guarded_tools))[:8],
        forbidden_patterns=list(policy.forbidden_patterns),
        bureaucracy_budget={
            "mode": bureaucracy_mode,
            "required_records": required_records_by_state.get(body.state, []),
            "optional_records": ["rule_candidate_marker", "tool_feedback"],
            "stage_evidence_format": "checkpoint:<change_id>",
            "avoid": avoid_by_state.get(body.state, []),
            "reason": "Use the smallest durable record that can satisfy the stage gate; reserve full reports for handoff and closeout.",
        },
        risk_controls=risk_controls,
        expected_outputs=list(policy.expected_outputs),
        next_transitions=list(policy.next_transitions),  # type: ignore[arg-type]
        reasons=reasons,
    )


async def build_task_execution_context(qdrant, body: TaskExecutionContextRequest) -> TaskExecutionContextResponse:
    policy = _STATE_POLICIES[body.state]
    laws = await list_project_laws(
        qdrant,
        project=body.project,
        status="all",
        include_promoted=True,
        limit=100,
    )
    laws = [law for law in laws if law.status in CONFIRMED_STATUSES]
    task_terms = _task_terms(body)
    required: list[TaskExecutionRuleRef] = []
    recommended: list[TaskExecutionRuleRef] = []
    project_testing_rules = _project_testing_rule_refs(body.project)
    if body.include_rules:
        if body.state == "verification":
            required.extend(project_testing_rules)
        else:
            recommended.extend(
                rule.model_copy(
                    update={
                        "reason": "Project-local testing rule is carried as durable project context for this task state."
                    }
                )
                for rule in project_testing_rules
            )
        for law in laws:
            text = _haystack(law)
            if _term_matches(text, policy.required_terms):
                required.append(_rule_ref(law, reason=f"Matched required terms for state '{body.state}'."))
            elif _term_matches(text, policy.recommended_terms) or _term_matches(text, task_terms):
                recommended.append(_rule_ref(law, reason=f"Matched recommended/task terms for state '{body.state}'."))

    required = required[: body.max_required_rules]
    recommended = recommended[: body.max_recommended_rules]
    tools = list(policy.tool_suggestions) if body.include_tools else []
    readiness = _build_readiness(body, laws=laws, project_testing_rules=project_testing_rules)
    risk_controls = list(policy.risk_controls)
    if body.state == "verification":
        contour_rules = _verification_contour_rules(laws)
        if contour_rules or project_testing_rules:
            titles = ", ".join(law.title for law in contour_rules[:3])
            if titles:
                risk_controls.append(f"Project-approved verification contour is defined by active law(s): {titles}.")
            tree_titles = ", ".join(rule.title for rule in project_testing_rules[:3])
            if tree_titles:
                risk_controls.append(f"Project-approved verification contour is defined by project knowledge: {tree_titles}.")
            if any("host pytest" in _haystack(law) for law in contour_rules):
                risk_controls.append("For this project, do not run host pytest when the approved contour forbids it.")
            if any("docker" in _haystack(law) for law in contour_rules):
                risk_controls.append("For this project, use the Docker-based verification contour described by project law.")
            tree_text = " ".join(
                f"{rule.title} {rule.rationale} {rule.topic_path or ''}"
                for rule in project_testing_rules
            ).casefold()
            if "host pytest" in tree_text:
                risk_controls.append("For this project, do not run host pytest when the approved contour forbids it.")
            if "docker" in tree_text:
                risk_controls.append("For this project, use the Docker-based verification contour described by project knowledge.")
        else:
            risk_controls.append("Verification contour unknown for this project; clarify or inspect before executing tests.")
    if body.state == "live_validation":
        restart_rules = [law for law in laws if _looks_like_restart_stale_window(law)]
        if restart_rules:
            titles = ", ".join(law.title for law in restart_rules[:3])
            risk_controls.append(
                f"Project restart validation is constrained by confirmed law(s): {titles}; wait the configured stale window before declaring startup broken."
            )
    operation_tray = _build_operation_tray(body, policy, tools, readiness, risk_controls) if body.include_tools else None
    return TaskExecutionContextResponse(
        project=body.project,
        state=body.state,
        task=body.task,
        intent=body.intent,
        readiness=readiness,
        operation_tray=operation_tray,
        required_rules=required,
        recommended_rules=recommended,
        recommended_tools=tools,
        risk_controls=risk_controls,
        expected_outputs=list(policy.expected_outputs),
        next_transitions=list(policy.next_transitions),  # type: ignore[arg-type]
        rationale=(
            "Explicit state MVP: generic state policy selects compact tool families, "
            "DB-backed active laws, risk controls, expected outputs, and legal next transitions."
        ),
        coverage={
            "active_laws_seen": len(laws),
            "required_rules": len(required),
            "recommended_rules": len(recommended),
            "tool_suggestions": len(tools),
            "risk_controls": len(risk_controls),
            "ready_to_enter": int(readiness.ready_to_enter),
            "missing_prerequisites": len(readiness.missing_prerequisites),
        },
    )
