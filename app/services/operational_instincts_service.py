from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.services.system_data_root import data_path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = data_path("operational_instincts.db")
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS operational_instincts (
    instinct_id         TEXT NOT NULL,
    layer               TEXT NOT NULL,
    scope_ref           TEXT NOT NULL DEFAULT '',
    family              TEXT NOT NULL DEFAULT 'core_bootstrap',
    phase               TEXT NOT NULL DEFAULT 'general',
    rank                TEXT NOT NULL,
    scope               TEXT NOT NULL,
    trigger             TEXT NOT NULL,
    action              TEXT NOT NULL,
    why_it_matters      TEXT NOT NULL,
    failure_if_missing  TEXT NOT NULL,
    language            TEXT NOT NULL DEFAULT 'en',
    active              INTEGER NOT NULL DEFAULT 1,
    activation_tags     TEXT NOT NULL DEFAULT '[]',
    updated_at          REAL NOT NULL,
    PRIMARY KEY (instinct_id, layer, scope_ref)
);

CREATE TABLE IF NOT EXISTS operational_instinct_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    context_type        TEXT NOT NULL,
    project_id          TEXT NOT NULL DEFAULT '',
    storage_trust_status TEXT NOT NULL DEFAULT '',
    code_inspection_recommended INTEGER NOT NULL DEFAULT 0,
    instinct_ids        TEXT NOT NULL DEFAULT '[]',
    families            TEXT NOT NULL DEFAULT '[]',
    phases              TEXT NOT NULL DEFAULT '[]',
    created_at          REAL NOT NULL
);
"""
_RANK_PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_LAYERS = ("global_builtin", "instance_local", "project_local")
_PHASE_ORDER = (
    "idea_capture",
    "task_framing",
    "option_selection",
    "pre_implementation",
    "post_implementation",
    "post_validation",
    "general",
    "release",
)
_PHASE_OBJECTIVES = {
    "idea_capture": "Capture valuable side ideas without losing the main execution thread.",
    "task_framing": "Turn an incomplete request into an explicit, bounded, and explainable task.",
    "option_selection": "Choose a solution path with explicit tradeoffs, cost awareness, and user alignment.",
    "pre_implementation": "Define the safest and smallest meaningful implementation step for the current iteration.",
    "post_implementation": "Check immediate scope drift and cheap regressions before broader validation.",
    "post_validation": "Validate, reframe if needed, and record outcomes so the work compounds.",
    "general": "Apply cross-cutting instincts that shape safe and truthful system behavior.",
    "release": "Prepare safe and honest defaults for public or fresh-instance use.",
}

_DEFAULT_INSTINCTS: dict[str, dict[str, Any]] = {
    "trust_first": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P0",
        "scope": "global",
        "trigger": "Any new task start or any answer that relies on memory or retrieval.",
        "action": "Check storage trust, integrity status, and data hygiene before treating memory output as reliable.",
        "why_it_matters": "MnemoForge can remain useful while parts of the substrate are degraded or contaminated.",
        "failure_if_missing": "The agent may produce confident answers from corrupted, stale, or service-polluted knowledge.",
        "language": "en",
        "active": True,
        "activation_tags": ["onboarding", "task_enrichment", "project_readiness", "bootstrap_checklist", "trust_degraded"],
    },
    "project_scope_first": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P0",
        "scope": "project",
        "trigger": "Any work involving project tasks, laws, docs, hints, improvements, memoirs, or coordination.",
        "action": "Always read and write with an explicit project_id when the task is project-specific.",
        "why_it_matters": "Project isolation depends on scope discipline, not on implicit context.",
        "failure_if_missing": "Cross-project leakage, mixed retrieval, and incorrect context assembly.",
        "language": "en",
        "active": True,
        "activation_tags": ["onboarding", "task_enrichment", "project_readiness", "bootstrap_checklist", "project"],
    },
    "ask_memory_before_code": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P0",
        "scope": "project",
        "trigger": "A new project task or investigation request.",
        "action": (
            "For any MnemoForge-backed project task, query MnemoForge before repository files, shell search, or ad-hoc "
            "memory reconstruction. Start with enrich_task_with_context using an explicit project_id. For priority, "
            "continuation, lifecycle, or open-work questions, call list_open_tasks, list_artifacts, or pull_task_context "
            "before inspecting files. For new or external projects, call get_project_readiness and bootstrap checklist "
            "surfaces before assuming project knowledge exists. Treat MnemoForge output as triage guidance: check degraded "
            "state, missing sources, data hygiene warnings, and whether results come from governed layers rather than raw "
            "memories. Inspect repository files only after MnemoForge narrows the question or reports missing/degraded context."
        ),
        "why_it_matters": (
            "MnemoForge is the project memory and coordination substrate; it carries task state, open improvements, "
            "checkpoints, laws, and operator intent that files alone cannot represent."
        ),
        "failure_if_missing": (
            "The agent answers project-priority, continuation, or architecture questions from markdown/status files alone "
            "while MCP is available, producing stale priorities and hidden assumptions."
        ),
        "language": "en",
        "active": True,
        "activation_tags": ["task_enrichment", "project_readiness", "project"],
    },
    "raw_is_not_knowledge": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P0",
        "scope": "storage",
        "trigger": "After ingest, client_scan, import, or any raw capture flow.",
        "action": "Do not treat raw memories as first-class project knowledge; verify projection into governed layers such as components, docs, tasks, and laws.",
        "why_it_matters": "Raw capture and governed knowledge serve different roles.",
        "failure_if_missing": "False readiness and misleading assumptions about project coverage.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_enrichment", "project_readiness", "bootstrap_checklist", "sparse_knowledge"],
    },
    "specific_data_lives_in_stores": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P0",
        "scope": "architecture",
        "trigger": "When a rule, fact, or guidance is slice-specific, project-specific, or operationally specific.",
        "action": "Keep general behavior in code and store specific guidance in project stores, integrity rules, hygiene policy data, or other dedicated data layers.",
        "why_it_matters": "Runtime code should carry mechanisms, not accidental local truths.",
        "failure_if_missing": "Hardcoded specifics, poor portability, and hidden drift between code and real operations.",
        "language": "en",
        "active": True,
        "activation_tags": ["onboarding"],
    },
    "degraded_must_be_visible": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P0",
        "scope": "global",
        "trigger": "Any fallback, bypass, degraded storage mode, or emergency path.",
        "action": "Surface the degraded state explicitly in health, admin, onboarding, and relevant APIs.",
        "why_it_matters": "A silent fallback hides the real health of the system.",
        "failure_if_missing": "Operators and agents assume the system is healthy when it is only limping.",
        "language": "en",
        "active": True,
        "activation_tags": ["onboarding", "project_readiness", "bootstrap_checklist", "trust_degraded", "remediation"],
    },
    "preview_before_repair": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P1",
        "scope": "operations",
        "trigger": "Any remediation, cleanup, quarantine, archive, or delete action.",
        "action": "Provide preview, dry-run, or report surfaces before apply.",
        "why_it_matters": "Operators need to see the intended blast radius before mutating live stores.",
        "failure_if_missing": "Destructive or poorly explained changes to live data.",
        "language": "en",
        "active": True,
        "activation_tags": ["remediation"],
    },
    "real_usage_beats_lab_confidence": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P1",
        "scope": "product",
        "trigger": "Validation of a new capability, architectural slice, or workflow.",
        "action": "Validate in real usage, with real workflows and live data, not only on self-case reasoning, synthetic traces, or isolated tests.",
        "why_it_matters": "For MnemoForge, real operation reveals product gaps that synthetic confidence hides.",
        "failure_if_missing": "False confidence in features that are not ready for real-world use.",
        "language": "en",
        "active": True,
        "activation_tags": ["validation", "onboarding", "project_readiness", "bootstrap_checklist"],
    },
    "system_must_explain_itself": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P1",
        "scope": "global",
        "trigger": "A new agent, a new instance, a new operator, or a new project bootstrap.",
        "action": "Expose onboarding, playbooks, readiness, workflow summaries, and self-describing operational guidance from within MnemoForge.",
        "why_it_matters": "Agents should learn how to use MnemoForge from MnemoForge itself.",
        "failure_if_missing": "Dependence on oral tradition, author memory, or hidden operational knowledge.",
        "language": "en",
        "active": True,
        "activation_tags": ["onboarding", "bootstrap_checklist"],
    },
    "unified_mcp_surface_first": {
        "family": "core_bootstrap",
        "phase": "task_framing",
        "rank": "P0",
        "scope": "product",
        "trigger": "A project task or search request needs current project state, recent work, or improvement context.",
        "action": "Prefer unified MCP surfaces such as normalize_mcp_intent, list_open_tasks, pull_task_context, reopen_task, report_task_checkpoint, list_artifacts, enrich-task, and review_improvement. For open work items, use list_open_tasks first; for task continuation, use pull_task_context for read-only checkpoint replay before claiming or mutating; use reopen_task only when a closed/inactive task must be made active again; use list_artifacts when you need broader artifact filtering. If you are unsure which MCP family to use, call normalize_mcp_intent first, then list_tool_families or tool_recommend before loading the full catalog. When any MCP tool is in testing, finish that tool's use-case with tool_feedback and include a compact evaluation envelope with scope, what_was_tested, expected_behavior, observed_behavior, friction, suggestion, and next_action when available. For task work, record a checkpoint at planning and every meaningful stage transition with report_task_checkpoint so interruptions do not lose progress. After completing a task or implementation, report the outcome back to MnemoForge with the linked improvement/task id, stage, and verdict when relevant; do not leave completion only in chat. Testing tools are auto-seeded for review and may be promoted or deprecated by background lifecycle review after enough time or feedback. Use specialized endpoints only when the unified surface cannot express the need; do not read project tables directly in agent workflows.",
        "why_it_matters": "Unified surfaces keep agents on the canonical path, reduce tool sprawl, and avoid schema-dependent shortcuts.",
        "failure_if_missing": "Agents bypass the main MCP value and fall back to fragmented endpoints or direct SQL/table inspection.",
        "language": "en",
        "active": True,
        "activation_tags": ["onboarding", "task_enrichment", "project_readiness", "bootstrap_checklist", "project"],
    },
    "progressive_disclosure_for_mcp_depth": {
        "family": "core_bootstrap",
        "phase": "task_framing",
        "rank": "P1",
        "scope": "product",
        "trigger": "The task can be started with the current project bundle, but deeper evidence may be needed.",
        "action": "Keep the first pass compact. Pull deeper MCP surfaces only when the current bundle is missing a needed answer, the task is ambiguous, or the requested operation requires it.",
        "why_it_matters": "MnemoForge stays useful when it narrows the path first and expands only on demand.",
        "failure_if_missing": "Agents overfetch context, inflate prompts, and lose the benefit of layered knowledge.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_enrichment", "project_readiness", "bootstrap_checklist", "project"],
    },
    "coordination_is_not_truth": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P1",
        "scope": "coordination",
        "trigger": "Inter-agent requests, responses, handoffs, and status updates.",
        "action": "Keep coordination messages separate from governed project truth unless explicitly promoted.",
        "why_it_matters": "Operational chatter is useful, but it is not automatically knowledge.",
        "failure_if_missing": "Knowledge pollution with temporary conversation exhaust.",
        "language": "en",
        "active": True,
        "activation_tags": ["onboarding", "coordination"],
    },
    "close_the_loop": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P1",
        "scope": "architecture",
        "trigger": "Any new subsystem or workflow.",
        "action": "Drive it to a full loop: detect, surface, act, verify, and record outcome.",
        "why_it_matters": "A surface without an outcome loop creates the illusion of completeness.",
        "failure_if_missing": "Half-built systems that look capable but are not operationally complete.",
        "language": "en",
        "active": True,
        "activation_tags": ["remediation", "validation"],
    },
    "safe_public_defaults": {
        "family": "core_bootstrap",
        "phase": "release",
        "rank": "P2",
        "scope": "release",
        "trigger": "Public alpha packaging, GitHub release preparation, or fresh-instance setup.",
        "action": "Ship safe defaults, a demo dataset, explicit disabled experimental modules, and no live service data.",
        "why_it_matters": "A public release should not depend on internal cleanup, hidden assumptions, or contaminated live stores.",
        "failure_if_missing": "Poor first-run experience, accidental leakage of operational data, and confusing alpha positioning.",
        "language": "en",
        "active": True,
        "activation_tags": ["release"],
    },
    "capture_ideas_without_context_switch": {
        "family": "task_lifecycle",
        "phase": "idea_capture",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A new idea, improvement, or concern appears while another task is active.",
        "action": "Record the idea immediately without switching away from the current task unless the idea materially changes it.",
        "why_it_matters": "Ideas should not be lost, but opportunistic switching destroys execution quality.",
        "failure_if_missing": "Ideas are forgotten or the main task is repeatedly abandoned.",
        "language": "en",
        "active": True,
        "activation_tags": ["idea_capture"],
    },
    "assess_idea_impact_before_switching": {
        "family": "task_lifecycle",
        "phase": "idea_capture",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A newly captured idea may affect the current task.",
        "action": "Classify the idea as non-blocking, priority-shifting, or architecture-changing before deciding to switch contexts.",
        "why_it_matters": "Not every good idea deserves immediate priority over active work.",
        "failure_if_missing": "The agent either overreacts to side ideas or misses a necessary pivot.",
        "language": "en",
        "active": True,
        "activation_tags": ["idea_capture", "task_framing"],
    },
    "every_task_must_exist_in_memory": {
        "family": "task_lifecycle",
        "phase": "task_framing",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A task becomes concrete enough to track.",
        "action": "Register the task in MnemoForge as a first-class work item before or at the start of execution.",
        "why_it_matters": "Tasks should survive beyond transient chat context.",
        "failure_if_missing": "Work becomes hard to continue, review, and compare over time.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_framing"],
    },
    "assume_initial_task_statement_is_incomplete": {
        "family": "task_lifecycle",
        "phase": "task_framing",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "The user states a new task, request, or problem.",
        "action": "Treat the initial problem statement as incomplete until constraints, assumptions, and expected outcomes are clarified.",
        "why_it_matters": "Users often omit details, rely on implicit context, or mis-specify the real target.",
        "failure_if_missing": "The agent solves the wrong problem cleanly.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_framing"],
    },
    "clarify_scope_assumptions_and_done": {
        "family": "task_lifecycle",
        "phase": "task_framing",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "Before committing to implementation.",
        "action": "Clarify scope, assumptions, constraints, and what counts as done.",
        "why_it_matters": "A task without explicit boundaries expands unpredictably.",
        "failure_if_missing": "Scope drift, premature coding, and endless revision loops.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_framing", "pre_implementation"],
    },
    "track_assumptions_explicitly": {
        "family": "task_lifecycle",
        "phase": "task_framing",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "The plan depends on inferred facts, missing constraints, or unverified user intent.",
        "action": "Record the assumptions shaping the current plan so they can be revisited when new evidence appears.",
        "why_it_matters": "Assumptions are often the hidden reason why a solution later needs reframing.",
        "failure_if_missing": "The reasoning chain becomes opaque and later corrections look arbitrary.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_framing"],
    },
    "rank_options_before_committing": {
        "family": "task_lifecycle",
        "phase": "option_selection",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "Multiple viable implementation paths exist.",
        "action": "Generate options and rank them by correctness, risk, cost, reversibility, maintainability, and strategic fit.",
        "why_it_matters": "Choosing an implementation path is itself a design decision.",
        "failure_if_missing": "The first plausible idea is mistaken for the best one.",
        "language": "en",
        "active": True,
        "activation_tags": ["option_selection"],
    },
    "cost_to_value_first": {
        "family": "task_lifecycle",
        "phase": "option_selection",
        "rank": "P0",
        "scope": "product",
        "trigger": "A new capability, enrichment layer, or automation path is being considered.",
        "action": "Estimate whether practical execution cost in latency, money, operator overhead, resource pressure, and failure risk is justified by the expected improvement in decision quality, resilience, or workflow speed.",
        "why_it_matters": "A theoretically elegant capability can still be product-negative if it costs more than it returns.",
        "failure_if_missing": "The system accumulates expensive, slow, or fragile AI layers that reduce practical usefulness.",
        "language": "en",
        "active": True,
        "activation_tags": ["option_selection"],
    },
    "escalate_capability_cost_only_when_roi_is_positive": {
        "family": "task_lifecycle",
        "phase": "option_selection",
        "rank": "P1",
        "scope": "product",
        "trigger": "A problem can be addressed by a cheaper simpler mechanism or by a more expensive heavier capability tier.",
        "action": "Start with the cheapest tier that can plausibly deliver decision-useful quality, and escalate only when the expected quality, resilience, or operational benefit is worth the added latency, cost, complexity, and reliability risk.",
        "why_it_matters": "Capability escalation should be economically and operationally justified, not reflexive.",
        "failure_if_missing": "The system either underbuilds with weak mechanisms or overuses expensive heavyweight layers for low-value work.",
        "language": "en",
        "active": True,
        "activation_tags": ["option_selection"],
    },
    "explain_to_reduce_knowledge_gap": {
        "family": "task_lifecycle",
        "phase": "option_selection",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "The agent knows materially more than the user about the solution space.",
        "action": "Explain the important tradeoffs and rationale before major commitment so the user can evaluate the path meaningfully.",
        "why_it_matters": "The user should understand the decision, not merely witness it.",
        "failure_if_missing": "The user cannot evaluate tradeoffs or catch hidden risks.",
        "language": "en",
        "active": True,
        "activation_tags": ["option_selection", "task_framing"],
    },
    "calibrate_dialogue_depth_to_user": {
        "family": "task_lifecycle",
        "phase": "task_framing",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "The user shows a particular level of domain familiarity, confidence, or confusion.",
        "action": "Adjust explanation depth, terminology, and decision framing to the user's observed understanding instead of using one fixed explanation style.",
        "why_it_matters": "Useful explanation depends on calibration, not on maximum verbosity.",
        "failure_if_missing": "The agent overexplains, underexplains, or misjudges how much context the user actually needs.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_framing", "option_selection"],
    },
    "implement_iteratively": {
        "family": "task_lifecycle",
        "phase": "pre_implementation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A task moves from planning into execution.",
        "action": "Prefer iterative delivery over pretending the first implementation will be final.",
        "why_it_matters": "Real tasks change once confronted with code, tests, and usage.",
        "failure_if_missing": "Large brittle implementations that ignore feedback.",
        "language": "en",
        "active": True,
        "activation_tags": ["pre_implementation"],
    },
    "capture_task_checkpoints_across_lifecycle": {
        "family": "task_lifecycle",
        "phase": "task_framing",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A task is being planned, worked on, blocked, paused, handed off, interrupted, or completed.",
        "action": "Record a compact task checkpoint at planning and every meaningful transition. Include stage, current status, summary, blockers, and next step. If the agent stops unexpectedly, the last checkpoint must still be recoverable in MnemoForge.",
        "why_it_matters": "Progress survives interruptions only when each stage leaves a durable trail.",
        "failure_if_missing": "Other agents cannot safely resume the work and the task disappears into chat history.",
        "language": "en",
        "active": True,
        "activation_tags": ["task_framing", "pre_implementation", "post_implementation", "post_validation"],
    },
    "define_done_for_this_iteration": {
        "family": "task_lifecycle",
        "phase": "pre_implementation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "Implementation is about to begin for the current slice of work.",
        "action": "Define what counts as sufficient completion for this iteration before building, including the intended verification step.",
        "why_it_matters": "Iterative work still needs an explicit finish line for the current slice.",
        "failure_if_missing": "Implementation expands indefinitely because the current iteration never has a clear stop condition.",
        "language": "en",
        "active": True,
        "activation_tags": ["pre_implementation"],
    },
    "prefer_smallest_proving_step": {
        "family": "task_lifecycle",
        "phase": "pre_implementation",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "A chosen solution path still needs evidence before a larger implementation slice.",
        "action": "Prefer the smallest real step that can meaningfully confirm or falsify the chosen path before expanding the implementation.",
        "why_it_matters": "Small proving steps reduce wasted work and surface bad assumptions earlier.",
        "failure_if_missing": "The agent commits to a large implementation before testing whether the path is actually sound.",
        "language": "en",
        "active": True,
        "activation_tags": ["pre_implementation"],
    },
    "use_reversible_first_steps": {
        "family": "task_lifecycle",
        "phase": "pre_implementation",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "A task involves architecture changes, data mutation, or other high-impact implementation decisions.",
        "action": "Choose early implementation steps that are easy to reverse, refine, or isolate while evidence is still incomplete.",
        "why_it_matters": "Reversibility lowers the cost of learning and reduces early overcommitment.",
        "failure_if_missing": "The first implementation move makes later correction expensive or disruptive.",
        "language": "en",
        "active": True,
        "activation_tags": ["pre_implementation", "remediation"],
    },
    "bounded_ownership_before_parallel_split": {
        "family": "task_lifecycle",
        "phase": "pre_implementation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "Work is about to be split across agents, CLIs, or bounded side packets.",
        "action": "Define an explicit owner_agent and bounded write_scope before delegating the packet so responsibility and modification boundaries are unambiguous.",
        "why_it_matters": "Parallel execution is only safe when ownership and write boundaries are explicit enough to avoid accidental overlap.",
        "failure_if_missing": "Parallel work collides, duplicates effort, or produces changes that are hard to merge safely.",
        "language": "en",
        "active": True,
        "activation_tags": ["pre_implementation", "coordination", "parallel_execution"],
    },
    "keep_parallel_packets_narrow": {
        "family": "task_lifecycle",
        "phase": "pre_implementation",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "A side branch or delegated packet is being prepared for another agent.",
        "action": "Prefer packets with one owned file, module, or similarly tight write scope unless a wider slice is clearly necessary.",
        "why_it_matters": "Narrow packets are easier to verify, merge back, and reason about under parallel execution.",
        "failure_if_missing": "Delegated work becomes fuzzy, expensive to verify, and more likely to conflict with adjacent changes.",
        "language": "en",
        "active": True,
        "activation_tags": ["pre_implementation", "coordination", "parallel_execution"],
    },
    "parallelize_only_with_mergeable_packets": {
        "family": "task_lifecycle",
        "phase": "pre_implementation",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "A task is being split into parallel packets or side branches.",
        "action": "Split work into packets only when their outputs can be merged back into the main thread without heavy manual reconciliation or ambiguous ownership.",
        "why_it_matters": "Parallel execution is valuable only when the return path is as safe and bounded as the delegation path.",
        "failure_if_missing": "The system creates parallel branches that are expensive to reconcile, causing context loss and merge friction.",
        "language": "en",
        "active": True,
        "activation_tags": ["pre_implementation", "coordination", "parallel_execution"],
    },
    "check_scope_and_regressions_before_full_validation": {
        "family": "task_lifecycle",
        "phase": "post_implementation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A code or configuration change has been made, but broader validation is not yet complete.",
        "action": "Check that the implementation still matches the intended iteration scope and look for immediate regressions before moving into broader validation.",
        "why_it_matters": "The step between implementation and full validation is where obvious drift and accidental side effects can still be caught cheaply.",
        "failure_if_missing": "The agent goes straight from coding to broad validation without noticing that the implementation already drifted from the intended slice.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_implementation"],
    },
    "reframe_after_new_evidence": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "Implementation, testing, or usage reveals new facts.",
        "action": "Return to task framing and update the problem statement when reality changes the task.",
        "why_it_matters": "Evidence can invalidate the original framing.",
        "failure_if_missing": "The agent keeps optimizing a stale interpretation of the problem.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_implementation", "post_validation"],
    },
    "validate_beyond_synthetic_tests": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A task appears implemented.",
        "action": "Validate not only with synthetic tests but also with real workflows, real operator paths, or real data when possible.",
        "why_it_matters": "Synthetic confidence often hides operational gaps.",
        "failure_if_missing": "The solution passes tests but fails in real use.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_implementation", "post_validation", "validation"],
    },
    "review_for_slop_and_false_plausibility": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "An implementation or explanation looks superficially convincing.",
        "action": "Review for neuroslop, decorative complexity, weak grounding, and vibe-coded plausibility without operational substance.",
        "why_it_matters": "LLM workflows can produce output that sounds right while remaining strategically weak.",
        "failure_if_missing": "Polished nonsense survives into the product.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_implementation", "post_validation"],
    },
    "validate_capability_layers_against_real_roi": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P1",
        "scope": "product",
        "trigger": "A technically sophisticated or resource-heavy capability appears functionally correct.",
        "action": "Validate the capability against real-world return on investment: quality gain, latency, cost, retry rate, resource pressure, throttling risk, operator trust, and failure impact.",
        "why_it_matters": "A capability is not successful just because it runs; it must remain worth operating.",
        "failure_if_missing": "The system keeps expensive or fragile layers whose real operating cost outweighs their practical value.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_implementation", "post_validation", "validation"],
    },
    "record_rejected_paths": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "A solution path has been chosen while other viable options were considered.",
        "action": "Record which alternative paths were rejected and why they were not chosen.",
        "why_it_matters": "Future agents need to know not only what was done, but why other plausible paths were declined.",
        "failure_if_missing": "The same rejected options return later without context, wasting time and causing repeated debate.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_validation", "option_selection"],
    },
    "close_with_outcome_and_followups": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A task reaches a meaningful completion point.",
        "action": "Record outcome, final decision, what worked, what failed, and follow-up tasks in MnemoForge.",
        "why_it_matters": "A finished task should improve future work rather than disappear into history.",
        "failure_if_missing": "The system repeats mistakes and loses hard-won knowledge.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_validation"],
    },
    "verify_then_close_packets": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P0",
        "scope": "workflow",
        "trigger": "A delegated or bounded task packet is reported as completed.",
        "action": "Verify the bounded result before closing the packet; completion claims alone are not enough.",
        "why_it_matters": "Packet workflows compound only when closure means verified completion rather than optimistic status changes.",
        "failure_if_missing": "Packets are closed on unverified claims and regressions slip into the main thread.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_validation", "coordination", "parallel_execution"],
    },
    "record_merge_back_trace": {
        "family": "task_lifecycle",
        "phase": "post_validation",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "A bounded packet result is being merged back into the main thread.",
        "action": "Record a short result_summary and verification_summary on packet closure so the merge-back remains inspectable after the code lands.",
        "why_it_matters": "Parallel work compounds better when packet closure carries a concise integration trace, not only a status change.",
        "failure_if_missing": "Closed packets lose their practical outcome and future agents must reconstruct the merge-back from code diffs or chat history.",
        "language": "en",
        "active": True,
        "activation_tags": ["post_validation", "coordination", "parallel_execution"],
    },
    "packet_lifecycle_must_remain_queryable": {
        "family": "core_bootstrap",
        "phase": "general",
        "rank": "P1",
        "scope": "workflow",
        "trigger": "Task packets are being paused, delegated, resumed, or closed across multiple agents.",
        "action": "Keep packet lifecycle status listable and inspectable so operators and agents can audit active, paused, and closed work safely.",
        "why_it_matters": "Parallel or resumable work stops being trustworthy when task state disappears into hidden chat history.",
        "failure_if_missing": "Operators lose track of in-flight packets and safe resume or audit becomes guesswork.",
        "language": "en",
        "active": True,
        "activation_tags": ["coordination", "parallel_execution", "onboarding"],
    },
}


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE_SQL)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(operational_instincts)").fetchall()
    }
    if "family" not in columns:
        conn.execute("ALTER TABLE operational_instincts ADD COLUMN family TEXT NOT NULL DEFAULT 'core_bootstrap'")
        conn.commit()
    if "phase" not in columns:
        conn.execute("ALTER TABLE operational_instincts ADD COLUMN phase TEXT NOT NULL DEFAULT 'general'")
        conn.commit()


def _normalize_layer(layer: str) -> str:
    layer = (layer or "").strip()
    if layer not in _LAYERS:
        raise ValueError(f"Unsupported layer: {layer}")
    return layer


def _row_to_instinct(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["active"] = bool(item.get("active", 1))
    tags_raw = item.get("activation_tags") or "[]"
    if isinstance(tags_raw, str):
        try:
            item["activation_tags"] = json.loads(tags_raw)
        except Exception:
            item["activation_tags"] = []
    else:
        item["activation_tags"] = list(tags_raw or [])
    return item


def _builtin_instincts() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for instinct_id, data in _DEFAULT_INSTINCTS.items():
        items.append(
            {
                "instinct_id": instinct_id,
                "layer": "global_builtin",
                "scope_ref": "",
                **data,
            }
        )
    return items


def list_operational_instincts(
    *,
    layer: str | None = None,
    scope_ref: str | None = None,
    family: str | None = None,
    phase: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    items = {(item["instinct_id"], item["layer"], item["scope_ref"]): item for item in _builtin_instincts()}
    with _connect() as conn:
        clauses: list[str] = []
        params: list[Any] = []
        if layer:
            clauses.append("layer = ?")
            params.append(layer)
        if scope_ref is not None:
            clauses.append("scope_ref = ?")
            params.append(scope_ref)
        if family:
            clauses.append("family = ?")
            params.append(family)
        if phase:
            clauses.append("phase = ?")
            params.append(phase)
        if active_only:
            clauses.append("active = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT instinct_id, layer, scope_ref, family, phase, rank, scope, trigger, action,
                   why_it_matters, failure_if_missing, language, active, activation_tags, updated_at
            FROM operational_instincts
            {where}
            ORDER BY instinct_id, layer, scope_ref
            """,
            params,
        ).fetchall()
    for row in rows:
        item = _row_to_instinct(row)
        items[(item["instinct_id"], item["layer"], item["scope_ref"])] = item
    out = list(items.values())
    if layer:
        out = [item for item in out if item["layer"] == layer]
    if scope_ref is not None:
        out = [item for item in out if item["scope_ref"] == scope_ref]
    if family:
        out = [item for item in out if item.get("family") == family]
    if phase:
        out = [item for item in out if item.get("phase") == phase]
    if active_only:
        out = [item for item in out if item["active"]]
    out.sort(key=lambda item: (_RANK_PRIORITY.get(item["rank"], 99), item["instinct_id"], item["layer"], item["scope_ref"]))
    return out


def upsert_operational_instinct(
    *,
    instinct_id: str,
    layer: str,
    scope_ref: str = "",
    family: str = "core_bootstrap",
    phase: str = "general",
    rank: str,
    scope: str,
    trigger: str,
    action: str,
    why_it_matters: str,
    failure_if_missing: str,
    language: str = "en",
    active: bool = True,
    activation_tags: list[str] | None = None,
) -> dict[str, Any]:
    layer = _normalize_layer(layer)
    now = time.time()
    payload = {
        "instinct_id": instinct_id,
        "layer": layer,
        "scope_ref": scope_ref or "",
        "family": family or "core_bootstrap",
        "phase": phase or "general",
        "rank": rank,
        "scope": scope,
        "trigger": trigger,
        "action": action,
        "why_it_matters": why_it_matters,
        "failure_if_missing": failure_if_missing,
        "language": language or "en",
        "active": bool(active),
        "activation_tags": list(activation_tags or []),
        "updated_at": now,
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO operational_instincts(
                instinct_id, layer, scope_ref, family, phase, rank, scope, trigger, action,
                why_it_matters, failure_if_missing, language, active, activation_tags, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instinct_id, layer, scope_ref) DO UPDATE SET
                family=excluded.family,
                phase=excluded.phase,
                rank=excluded.rank,
                scope=excluded.scope,
                trigger=excluded.trigger,
                action=excluded.action,
                why_it_matters=excluded.why_it_matters,
                failure_if_missing=excluded.failure_if_missing,
                language=excluded.language,
                active=excluded.active,
                activation_tags=excluded.activation_tags,
                updated_at=excluded.updated_at
            """,
            (
                payload["instinct_id"],
                payload["layer"],
                payload["scope_ref"],
                payload["family"],
                payload["phase"],
                payload["rank"],
                payload["scope"],
                payload["trigger"],
                payload["action"],
                payload["why_it_matters"],
                payload["failure_if_missing"],
                payload["language"],
                1 if payload["active"] else 0,
                json.dumps(payload["activation_tags"], ensure_ascii=False),
                payload["updated_at"],
            ),
        )
        conn.commit()
    return payload


def _merged_instincts(*, project_id: str | None = None) -> list[dict[str, Any]]:
    merged = {item["instinct_id"]: item for item in _builtin_instincts()}
    with _connect() as conn:
        instance_rows = conn.execute(
            """
            SELECT instinct_id, layer, scope_ref, rank, scope, trigger, action,
                   family, phase, why_it_matters, failure_if_missing, language, active, activation_tags, updated_at
            FROM operational_instincts
            WHERE layer = 'instance_local' AND scope_ref = ''
            """
        ).fetchall()
        for row in instance_rows:
            item = _row_to_instinct(row)
            merged[item["instinct_id"]] = item
        if project_id:
            project_rows = conn.execute(
                """
                SELECT instinct_id, layer, scope_ref, rank, scope, trigger, action,
                       family, phase, why_it_matters, failure_if_missing, language, active, activation_tags, updated_at
                FROM operational_instincts
                WHERE layer = 'project_local' AND scope_ref = ?
                """,
                (project_id,),
            ).fetchall()
            for row in project_rows:
                item = _row_to_instinct(row)
                merged[item["instinct_id"]] = item
    return list(merged.values())


def _context_tags(
    *,
    context_type: str,
    project_id: str | None = None,
    storage_trust_status: str | None = None,
    code_inspection_recommended: bool = False,
) -> set[str]:
    tags = {context_type}
    if project_id:
        tags.add("project")
    if storage_trust_status and storage_trust_status != "ok":
        tags.add("trust_degraded")
    if code_inspection_recommended:
        tags.add("sparse_knowledge")
    if context_type == "onboarding":
        tags.add("session_start")
    if context_type in {"project_readiness", "bootstrap_checklist"}:
        tags.add("validation")
        tags.add("bootstrap")
    return tags


def get_active_operational_instincts(
    *,
    context_type: str,
    project_id: str | None = None,
    storage_trust_status: str | None = None,
    code_inspection_recommended: bool = False,
    limit: int = 5,
    record_activation: bool = True,
) -> list[dict[str, Any]]:
    tags = _context_tags(
        context_type=context_type,
        project_id=project_id,
        storage_trust_status=storage_trust_status,
        code_inspection_recommended=code_inspection_recommended,
    )
    matched: list[dict[str, Any]] = []
    for item in _merged_instincts(project_id=project_id):
        if not item.get("active", True):
            continue
        activation_tags = set(item.get("activation_tags") or [])
        if not activation_tags.intersection(tags):
            continue
        matched.append(item)
    matched.sort(key=lambda item: (_RANK_PRIORITY.get(item["rank"], 99), item["instinct_id"]))
    selected = matched[:limit]
    if record_activation:
        record_operational_instinct_activation(
            context_type=context_type,
            project_id=project_id,
            storage_trust_status=storage_trust_status,
            code_inspection_recommended=code_inspection_recommended,
            instincts=selected,
        )
    return selected


def record_operational_instinct_activation(
    *,
    context_type: str,
    project_id: str | None,
    storage_trust_status: str | None,
    code_inspection_recommended: bool,
    instincts: list[dict[str, Any]],
) -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO operational_instinct_events(
                context_type, project_id, storage_trust_status, code_inspection_recommended,
                instinct_ids, families, phases, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context_type,
                project_id or "",
                storage_trust_status or "",
                1 if code_inspection_recommended else 0,
                json.dumps([item["instinct_id"] for item in instincts], ensure_ascii=False),
                json.dumps(sorted(set(item.get("family") or "" for item in instincts if item.get("family"))), ensure_ascii=False),
                json.dumps(sorted(set(item.get("phase") or "" for item in instincts if item.get("phase"))), ensure_ascii=False),
                now,
            ),
        )
        conn.commit()


def build_operational_instinct_activation_summary(*, limit: int = 200) -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT context_type, project_id, instinct_ids, families, phases, created_at
            FROM operational_instinct_events
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    events = [dict(row) for row in rows]
    by_context: dict[str, int] = {}
    by_family: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    instinct_counts: dict[str, int] = {}
    for row in events:
        context_type = str(row.get("context_type") or "")
        by_context[context_type] = by_context.get(context_type, 0) + 1
        for family in json.loads(row.get("families") or "[]"):
            by_family[family] = by_family.get(family, 0) + 1
        for phase in json.loads(row.get("phases") or "[]"):
            by_phase[phase] = by_phase.get(phase, 0) + 1
        for instinct_id in json.loads(row.get("instinct_ids") or "[]"):
            instinct_counts[instinct_id] = instinct_counts.get(instinct_id, 0) + 1
    top_instincts = [
        {"instinct_id": instinct_id, "activations": count}
        for instinct_id, count in sorted(instinct_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]
    return {
        "recent_event_count": len(events),
        "by_context": by_context,
        "by_family": by_family,
        "by_phase": by_phase,
        "top_instincts": top_instincts,
        "events": events[:20],
    }


def build_operational_instinct_playbook(
    *,
    family: str,
    project_id: str | None = None,
    active_only: bool = True,
) -> dict[str, Any]:
    if project_id:
        items = [
            item
            for item in _merged_instincts(project_id=project_id)
            if item.get("family") == family and (item.get("active", True) or not active_only)
        ]
    else:
        items = list_operational_instincts(family=family, active_only=active_only)
    items.sort(key=lambda item: (_RANK_PRIORITY.get(item["rank"], 99), item["phase"], item["instinct_id"]))
    phase_map: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        phase_map.setdefault(str(item.get("phase") or "general"), []).append(item)
    ordered_phases = [phase for phase in _PHASE_ORDER if phase in phase_map]
    ordered_phases.extend(sorted(phase for phase in phase_map if phase not in ordered_phases))
    phases: list[dict[str, Any]] = []
    for phase in ordered_phases:
        phase_items = phase_map[phase]
        by_rank: dict[str, int] = {}
        for item in phase_items:
            rank = str(item.get("rank") or "P3")
            by_rank[rank] = by_rank.get(rank, 0) + 1
        core_instinct_ids = [item["instinct_id"] for item in phase_items if item.get("rank") == "P0"]
        supporting_instinct_ids = [item["instinct_id"] for item in phase_items if item.get("rank") != "P0"]
        phases.append(
            {
                "phase": phase,
                "objective": _PHASE_OBJECTIVES.get(phase, ""),
                "priority_summary": by_rank,
                "core_instinct_ids": core_instinct_ids,
                "supporting_instinct_ids": supporting_instinct_ids,
                "instinct_ids": [item["instinct_id"] for item in phase_items],
                "instincts": phase_items,
            }
        )
    return {
        "family": family,
        "project_id": project_id or "",
        "active_only": active_only,
        "phase_sequence": ordered_phases,
        "phase_count": len(phases),
        "total_instincts": len(items),
        "phases": phases,
    }


def render_operational_instincts_block(instincts: list[dict[str, Any]], *, heading: str = "## Active Operational Instincts") -> str:
    if not instincts:
        return ""
    lines = [heading, ""]
    for item in instincts:
        lines.append(f"- [{item['rank']}] {item['instinct_id']}")
        lines.append(f"  Action: {item['action']}")
        lines.append(f"  Why: {item['why_it_matters']}")
    return "\n".join(lines)


def render_onboarding_instincts_block(instincts: list[dict[str, Any]]) -> str:
    if not instincts:
        return ""
    lines = ["ACTIVE OPERATIONAL INSTINCTS:"]
    for item in instincts:
        lines.append(f"  - [{item['rank']}] {item['instinct_id']}: {item['action']}")
    return "\n".join(lines)
