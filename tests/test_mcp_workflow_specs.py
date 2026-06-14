from __future__ import annotations

import json
from pathlib import Path

from app.services.mcp_mailbox import (
    build_mailbox_get_packet,
    build_mailbox_state_packet,
    build_mailbox_submit_receipt,
    evaluate_mailbox_postconditions,
    mailbox_form_by_id,
)
from app.services.mcp_mailbox_read import (
    MailboxReadDependencies,
    build_mailbox_get_response,
    build_mailbox_state_response,
)
from app.services import mcp_simple_read_actions
from app.services.mcp_simple_read_actions import (
    PublicRefDependencies,
    SimpleReadDependencies,
    build_simple_get_query_response,
    build_simple_public_ref_response,
)
from app.services.public_ref_index import AmbiguousPublicRefError, get_public_ref_index_store
from app.services.mcp_simple_surface_actions import compact_resource_result
from app.services.cognitive_health_service import build_cognitive_health_packet
from app.services.context_cue_service import context_cues_for_query, context_cues_for_state
from app.services.evidence_classification_service import classify_evidence_items
from app.services import planning_advisor_service
from app.services.public_diagnostic_service import (
    attach_public_diagnostic_incident,
    build_public_diagnostic_incident,
)
from app.services.mcp_workflow_specs import (
    list_mailbox_forms_for_state,
    load_clerk_capture_registry,
    load_feature_toggle_registry,
    load_named_json_spec,
    load_mailbox_form_policy_spec,
    load_packet_template,
    load_response_envelope_spec,
    load_route_catalog_spec,
    load_runtime_profile_spec,
    load_state_spec,
    load_task_lease_spec,
    load_tool_contract_catalog_spec,
    load_tool_family_registry,
    load_tool_surface_spec,
    validate_specs,
)
from app.services.stage_applicability_service import stage_allows_block
from app.services.spec_edit_guardrail_service import audit_universal_spec_runtime_leaks


MCP_SPEC_ROOT = Path(__file__).resolve().parents[1] / "app" / "mcp_specs"


def test_mcp_specs_use_ascii_internal_language() -> None:
    non_ascii_files = []
    for path in sorted(MCP_SPEC_ROOT.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        if any(ord(char) > 127 for char in content):
            non_ascii_files.append(str(path.relative_to(MCP_SPEC_ROOT)))

    assert non_ascii_files == []


def test_universal_specs_do_not_encode_project_specific_runtime_details() -> None:
    findings = audit_universal_spec_runtime_leaks(spec_root=MCP_SPEC_ROOT)

    assert findings == []


def test_default_workflow_specs_validate() -> None:
    summary = validate_specs()

    assert {
        "planning",
        "implementation",
        "verification",
        "live_validation",
        "checkpointing",
        "handoff",
        "operator_review",
    } <= set(summary["states"])
    assert "improvement" in summary["clerk_capture_types"]
    assert "architecture_principle" in summary["clerk_capture_types"]
    assert "project_capture_facade" in summary["feature_toggles"]
    assert "weak_mcp_operator" in summary["runtime_profile_presets"]
    assert "same_fingerprint_after_ttl" in summary["task_reclaim_policies"]
    assert "forms" in summary["response_public_fields"]
    assert "route_id" in summary["response_internal_fields"]
    assert {"mailbox_state", "mailbox_submit", "mailbox_get"} <= set(summary["mailbox_actions"])
    assert "planning" in summary["mailbox_form_policy_states"]
    assert "minimal" in summary["mailbox_form_visibility_profiles"]
    assert summary["route_catalogs"] == [
        "project_work",
        "project_rules",
        "project_context",
        "project_verify",
        "project_capture",
    ]
    assert "pull_task_context" in summary["project_work_route_intents"]
    assert "propose_law" in summary["project_rules_route_intents"]
    assert "tool_discovery" in summary["tool_families"]
    assert summary["tool_surface_public_entrypoints"] == ["help", "state", "get", "submit"]
    assert summary["public_tool_contracts"] == ["help", "state", "get", "submit", "put"]
    assert summary["discovery_tool_contracts"] == [
        "list_tool_families",
        "tool_family_tools",
        "tool_explain",
        "tool_recommend",
    ]
    assert summary["mailbox_tool_contracts"] == ["mailbox_state", "mailbox_submit", "mailbox_get"]
    assert summary["instruction_tool_contracts"] == ["load_instruction_layer", "list_instruction_layers"]
    assert summary["learning_review_tool_contracts"] == [
        "list_learning_candidates",
        "approve_learning_candidate",
        "defer_learning_candidate",
        "reject_learning_candidate",
    ]
    assert summary["improvement_review_tool_contracts"] == ["review_improvement"]
    assert summary["project_identity_tool_contracts"] == ["list_project_aliases", "rename_project"]
    assert summary["workflow_helper_tool_contracts"] == [
        "normalize_mcp_intent",
        "project_workflow",
        "project_workflow_submit",
        "reopen_task",
    ]
    assert summary["project_context_execution_tool_contracts"] == [
        "enrich_task_with_context",
        "get_task_execution_context",
        "operational_tray",
    ]
    assert summary["project_knowledge_core_tool_contracts"] == [
        "upsert_knowledge_tree_node",
        "get_project_readiness",
        "get_project_bootstrap_checklist",
        "get_project_reconstruction_bundle",
    ]
    assert summary["remote_snapshot_tool_contracts"] == ["plan_remote_snapshot", "sync_remote_snapshot"]
    assert summary["storage_trust_tool_contracts"] == ["get_storage_trust_status"]
    assert summary["coordination_message_tool_contracts"] == [
        "send_coordination_message",
        "pickup_coordination_messages",
        "list_coordination_messages",
        "update_coordination_message_status",
    ]
    assert summary["governance_feedback_tool_contracts"] == [
        "set_canonical_status",
        "merge_canonicals",
        "fix_layout_feedback",
    ]
    assert summary["artifact_navigation_tool_contracts"] == [
        "get_artifact",
        "list_artifacts",
        "list_open_tasks",
        "resolve_artifact",
        "reopen_artifact",
    ]
    assert {
        "claim_task",
        "close_task",
        "confirm_law",
        "create_law",
        "create_improvement",
        "finish_task",
        "record_progress",
        "release_task_claim",
        "run_verification",
        "start_task",
        "store_memory",
        "get_task_context",
        "set_feature_gate",
        "knowledge_refinement_feedback",
    } <= set(summary["mailbox_forms"])


def test_evidence_classification_distinguishes_code_verification_and_live_diagnostic() -> None:
    verification = classify_evidence_items(["Formal code verification passed through the approved verification contour."])
    live = classify_evidence_items(["Live diagnostic telemetry reviewed on the working database."])
    mixed = classify_evidence_items([
        "Formal code verification passed.",
        "Live diagnostic telemetry reviewed on the working database.",
    ])

    assert verification["kind"] == "code_verification"
    assert verification["verification_evidence"] is True
    assert live["kind"] == "live_diagnostic"
    assert live["live_diagnostic"] is True
    assert mixed["kind"] == "mixed"


def test_verification_state_blocks_unresolved_host_execution() -> None:
    spec = load_state_spec("verification")

    host_execution = next(pattern for pattern in spec.forbidden_patterns if pattern.id == "unresolved_host_execution")
    assert "host execution_context" in host_execution.match
    assert "project-approved verification contour" in host_execution.message


def test_state_packet_template_is_llm_facing_and_compact() -> None:
    spec = load_state_spec("planning")
    template = load_packet_template(spec.packet.template)

    assert spec.packet.compact is True
    assert "{{allowed_tools}}" in template
    assert "{{forbidden_patterns}}" in template
    assert "Next Safe Action" in template


def test_clerk_capture_types_do_not_collapse_improvements_into_checkpoints() -> None:
    registry = load_clerk_capture_registry()
    by_id = {item.id: item for item in registry.capture_types}

    assert by_id["task_checkpoint"].mutation_tool == "record_task_checkpoint"
    assert by_id["improvement"].mutation_tool == "record_work_result"
    assert by_id["architecture_principle"].mutation_tool == "memory_store"
    assert "mcp-improvement" in by_id["improvement"].anchor_tags


def test_feature_toggles_can_quarantine_broken_capture_paths() -> None:
    registry = load_feature_toggle_registry()
    by_id = {item.id: item for item in registry.toggles}

    project_capture = by_id["project_capture_facade"]
    clerk = by_id["clerk_checkpoint_draft"]

    assert project_capture.default_enabled is True
    assert "session" in project_capture.scopes
    assert "project_capture" in project_capture.target_tools
    assert "record_work_result" in project_capture.replacement_tools
    assert "misroute" in project_capture.disable_reason
    assert "clerk_draft_report" in clerk.target_tools
    assert "runtime_profile" in project_capture.scopes


def test_states_reference_known_feature_toggles() -> None:
    planning = load_state_spec("planning")
    checkpointing = load_state_spec("checkpointing")

    assert [toggle.id for toggle in planning.feature_toggles] == ["project_capture_facade"]
    assert {toggle.id for toggle in checkpointing.feature_toggles} == {
        "clerk_checkpoint_draft",
        "project_capture_facade",
    }


def test_runtime_profile_distinguishes_agent_label_from_cli_model_fingerprint() -> None:
    spec = load_runtime_profile_spec()
    fields = {field.name: field for field in spec.fingerprint_fields}
    presets = {preset.id: preset for preset in spec.profile_presets}

    assert spec.stable_identity_file == ".mnemoforge/agent_identity.json"
    assert fields["workspace_root"].privacy == "hashed"
    assert fields["client_name"].required is True
    assert fields["model_name"].source == "model"
    assert "project_capture_facade" in presets["weak_mcp_operator"].default_disabled_features
    assert presets["strong_mcp_operator"].default_disabled_features == []
    assert presets["weak_mcp_operator"].allow_internal_diagnostics is False
    assert presets["diagnostic_operator"].allow_internal_diagnostics is True


def test_task_reclaim_policy_uses_fingerprint_not_agent_label_only() -> None:
    spec = load_task_lease_spec()
    by_id = {policy.id: policy for policy in spec.reclaim_policies}
    ttl = by_id["same_fingerprint_after_ttl"]
    session_crash = by_id["same_fingerprint_after_session_crash"]

    assert "agent_id" in spec.claim_identity_fields
    assert "agent_fingerprint" in spec.claim_identity_fields
    assert spec.primary_ownership_key == "agent_fingerprint"
    assert "work_id identifies one concrete work session" in spec.work_id_semantics
    assert "agent_fingerprint" in ttl.ownership_keys
    assert "incoming_agent_fingerprint_matches_latest_claim" in ttl.allow_when
    assert "incoming_agent_fingerprint_missing" in ttl.deny_when
    assert "active_lease_owned_by_different_fingerprint" in ttl.deny_when
    assert "force_release_task_claim" in ttl.replacement_for
    assert "work_token_hash" in session_crash.ownership_keys
    assert "danger_mode_session_bypass" in session_crash.replacement_for


def test_response_envelope_filters_internal_logistics_by_default() -> None:
    spec = load_response_envelope_spec()
    public = {field.name for field in spec.public_fields}
    internal = {field.name for field in spec.internal_fields}

    assert spec.default_visibility == "public"
    assert {"state", "instruction", "forms", "receipt", "next_safe_action"} <= public
    assert {"route_id", "internal_tool", "expected_metadata", "actual_metadata"} <= internal
    assert "diagnostic_operator" in spec.diagnostic_access_profiles
    assert not public & internal


def test_mailbox_forms_include_postconditions_for_health_detection() -> None:
    planning_forms = {form.id: form for form in list_mailbox_forms_for_state("planning")}
    verification_forms = {form.id: form for form in list_mailbox_forms_for_state("verification")}

    create_improvement = planning_forms["create_improvement"]
    run_verification = verification_forms["run_verification"]

    assert create_improvement.postconditions.expected_metadata["artifact_type"] == "improvement"
    assert "clerk_draft_report" in create_improvement.postconditions.forbidden_metadata["internal_tool"]
    assert run_verification.postconditions.expected_metadata["result_kind"] == "verification_contour"
    assert "command" not in run_verification.postconditions.forbidden_metadata
    assert "data_ref" in run_verification.postconditions.required_receipt_fields


def test_mailbox_form_policy_is_declarative_priority_and_visibility_source() -> None:
    policy = load_mailbox_form_policy_spec()
    minimal_rule = next(rule for rule in policy.visibility_rules if rule.packet_profile == "minimal")
    minimal_limit = next(limit for limit in policy.packet_limits if limit.packet_profile == "minimal")

    assert policy.state_priorities["planning"][:2] == ["get_task_context", "start_task"]
    assert policy.state_priorities["planning"].index("claim_task") > policy.state_priorities["planning"].index("start_task")
    assert policy.state_priorities["planning"].index("create_improvement") < policy.state_priorities["planning"].index("record_progress")
    assert minimal_rule.hidden_form_ids == ["claim_task"]
    assert minimal_rule.hide_only_when_form_ids_available == ["start_task"]
    assert minimal_limit.max_forms == 5


def test_project_work_route_catalog_keeps_route_examples_out_of_service_code() -> None:
    catalog = load_route_catalog_spec("project_work")
    routes = {route.intent_type: route for route in catalog.routes}

    assert catalog.facade == "project_work"
    assert routes["next_priority"].tool == "list_open_tasks"
    assert "what should i do next" in routes["next_priority"].examples
    assert routes["reject_checkpoint_draft"].mutating is True


def test_project_rules_route_catalog_keeps_scoring_hints_in_spec_data() -> None:
    catalog = load_route_catalog_spec("project_rules")
    routes = {route.intent_type: route for route in catalog.routes}

    assert routes["propose_law"].arg_bonus == ["title", "statement"]
    assert routes["list_laws"].bonus_terms == ["law", "laws", "rule", "rules"]


def test_context_verify_and_capture_route_catalogs_are_declarative() -> None:
    context_routes = {route.intent_type: route for route in load_route_catalog_spec("project_context").routes}
    verify_routes = {route.intent_type: route for route in load_route_catalog_spec("project_verify").routes}
    capture_routes = {route.intent_type: route for route in load_route_catalog_spec("project_capture").routes}

    assert context_routes["task_details"].arg_bonus == ["task_id"]
    assert verify_routes["restart_validation_plan"].bonus_terms == ["restart", "live", "server"]
    assert capture_routes["record_work_result"].arg_bonus == ["summary", "verification", "changed_files"]


def test_tool_family_discovery_metadata_is_declarative() -> None:
    registry = load_tool_family_registry()
    families = {family.id: family for family in registry.families}

    assert families["tool_discovery"].entrypoints[:2] == ["list_tool_families", "tool_family_tools"]
    assert "get" not in families["project_knowledge"].preferred_tools
    assert "memory_search" in families["memory_operations"].preferred_tools


def test_tool_surface_priority_is_declarative() -> None:
    spec = load_tool_surface_spec()

    assert spec.public_entrypoints == ["help", "state", "get", "submit"]
    assert "mailbox_get" in spec.compatibility_tools
    assert spec.compact_tool_names[:4] == spec.public_entrypoints


def test_public_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("public_surface")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert list(contracts)[:4] == ["help", "state", "get", "submit"]
    assert contracts["get"].inputSchema["properties"]["response_format"]["default"] == "auto"
    assert contracts["put"].description.startswith("Compatibility alias")


def test_read_only_discovery_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("discovery_read")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["tool_family_tools"].inputSchema["required"] == ["family"]
    assert contracts["tool_recommend"].inputSchema["properties"]["top_n"]["maximum"] == 5


def test_mailbox_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("mailbox_protocol")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["mailbox_state"].inputSchema["required"] == ["state"]
    assert contracts["mailbox_submit"].inputSchema["required"] == ["form_id", "payload"]
    assert contracts["mailbox_get"].inputSchema["properties"]["ref"]["type"] == "string"


def test_instruction_layer_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("instruction_layers")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["load_instruction_layer"].inputSchema["required"] == ["layer"]
    assert contracts["load_instruction_layer"].inputSchema["properties"]["layer"]["enum"] == ["L3", "L4"]
    assert contracts["list_instruction_layers"].inputSchema["properties"]["layer"]["enum"] == ["L2", "L3", "L4"]


def test_learning_review_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("learning_review")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["list_learning_candidates"].inputSchema["properties"]["limit"]["maximum"] == 100
    assert contracts["approve_learning_candidate"].inputSchema["required"] == ["artifact_id"]
    assert contracts["defer_learning_candidate"].inputSchema["properties"]["defer_days"]["maximum"] == 90
    assert contracts["reject_learning_candidate"].inputSchema["properties"]["rejection_source"]["type"] == "string"


def test_improvement_review_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("improvement_review")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["review_improvement"].inputSchema["required"] == ["improvement_id"]
    assert contracts["review_improvement"].inputSchema["properties"]["stage"]["enum"] == [
        "proposal",
        "beta_test",
        "experimental",
        "stable",
        "deprecated",
    ]
    assert contracts["review_improvement"].inputSchema["properties"]["verdict"]["enum"] == [
        "effective",
        "ineffective",
    ]


def test_project_identity_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("project_identity")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["list_project_aliases"].inputSchema["properties"]["project_id"]["type"] == "string"
    assert contracts["rename_project"].inputSchema["required"] == ["old_project_id", "new_project_id"]
    assert contracts["rename_project"].inputSchema["properties"]["apply"]["default"] is False
    assert contracts["rename_project"].inputSchema["properties"]["ensure_alias"]["default"] is True


def test_workflow_helper_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("workflow_helpers")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["normalize_mcp_intent"].inputSchema["required"] == ["intent"]
    assert contracts["normalize_mcp_intent"].inputSchema["properties"]["top_n"]["maximum"] == 5
    assert contracts["project_workflow"].inputSchema["properties"]["workflow"]["enum"] == ["task_completion"]
    assert contracts["project_workflow_submit"].inputSchema["required"] == ["workflow", "form"]
    assert contracts["reopen_task"].inputSchema["properties"]["status"]["default"] == "active"


def test_project_context_execution_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("project_context_execution")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["enrich_task_with_context"].inputSchema["required"] == ["project_id", "task"]
    assert contracts["enrich_task_with_context"].inputSchema["properties"]["context_profile"]["enum"] == [
        "default",
        "handoff_compact",
        "hot_path",
    ]
    assert contracts["get_task_execution_context"].inputSchema["required"] == ["task", "state"]
    assert contracts["get_task_execution_context"].inputSchema["properties"]["include_rules"]["default"] is True
    assert contracts["operational_tray"].inputSchema["properties"]["action"]["enum"] == ["inspect", "execute"]
    assert "record_checkpoint" in contracts["operational_tray"].inputSchema["properties"]["tray_action"]["enum"]


def test_project_knowledge_core_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("project_knowledge_core")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["upsert_knowledge_tree_node"].inputSchema["required"] == ["topic_path", "title"]
    assert contracts["upsert_knowledge_tree_node"].inputSchema["properties"]["type"]["default"] == "area"
    assert contracts["get_project_readiness"].inputSchema["required"] == ["project_id"]
    assert contracts["get_project_bootstrap_checklist"].inputSchema["required"] == ["project_id"]
    assert contracts["get_project_reconstruction_bundle"].inputSchema["properties"]["max_items_per_layer"]["maximum"] == 50


def test_remote_snapshot_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("remote_snapshot")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["plan_remote_snapshot"].inputSchema["required"] == ["project_id", "snapshot"]
    assert contracts["sync_remote_snapshot"].inputSchema["required"] == ["project_id", "snapshot"]
    plan_props = contracts["plan_remote_snapshot"].inputSchema["properties"]
    assert plan_props["storage_mode"]["enum"] == ["knowledge_only", "selective_source_cache", "full_mirror"]
    assert plan_props["snapshot"]["properties"]["source_mode"]["enum"] == [
        "workspace",
        "git_snapshot",
        "github_pr",
        "archive_bundle",
    ]
    assert plan_props["files"]["items"]["required"] == ["path", "status"]


def test_storage_trust_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("storage_trust")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["get_storage_trust_status"].inputSchema["type"] == "object"
    assert contracts["get_storage_trust_status"].inputSchema["properties"] == {}


def test_coordination_message_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("coordination_messages")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["send_coordination_message"].inputSchema["required"] == [
        "project",
        "from_agent",
        "to_agent",
        "content",
    ]
    assert contracts["send_coordination_message"].inputSchema["properties"]["message_type"]["default"] == "question"
    assert contracts["pickup_coordination_messages"].inputSchema["required"] == ["agent_id"]
    assert contracts["list_coordination_messages"].inputSchema["properties"]["mailbox"]["enum"] == [
        "inbox",
        "outbox",
        "thread",
    ]
    assert contracts["update_coordination_message_status"].inputSchema["required"] == [
        "message_id",
        "status",
        "acted_by",
    ]


def test_governance_feedback_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("governance_feedback")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["set_canonical_status"].inputSchema["required"] == ["canonical_id", "suppressed"]
    assert contracts["merge_canonicals"].inputSchema["required"] == ["source_id", "target_id"]
    assert contracts["fix_layout_feedback"].inputSchema["required"] == ["correction_id", "confirmed"]
    assert contracts["fix_layout_feedback"].inputSchema["properties"]["confirmed"]["type"] == "boolean"


def test_artifact_navigation_tool_contracts_are_declarative() -> None:
    catalog = load_tool_contract_catalog_spec("artifact_navigation")
    contracts = {tool.name: tool for tool in catalog.tools}

    assert contracts["get_artifact"].inputSchema["required"] == ["artifact_key"]
    assert contracts["list_artifacts"].inputSchema["properties"]["limit"]["maximum"] == 100
    assert contracts["list_open_tasks"].inputSchema["properties"]["claim_filter"]["default"] == "available"
    assert contracts["list_open_tasks"].inputSchema["properties"]["assignment_filter"]["enum"] == [
        "all",
        "independent",
        "needs_review",
    ]
    assert contracts["resolve_artifact"].inputSchema["required"] == ["artifact_key"]
    assert contracts["reopen_artifact"].inputSchema["required"] == ["artifact_key", "project"]


def test_mailbox_state_packet_is_public_only_for_weak_profiles() -> None:
    packet = build_mailbox_state_packet(
        state="planning",
        project="mnemoforge",
        runtime_profile_id="weak_mcp_operator",
        diagnostic=True,
    )

    assert packet["state"] == "planning"
    assert "_internal" not in packet
    assert any(form["form_id"] == "create_improvement" for form in packet["forms"])
    assert any(form["form_id"] == "start_task" for form in packet["forms"])
    assert not any(form["form_id"] == "claim_task" for form in packet["forms"])
    assert "claim_task" in packet["hidden_forms"]
    assert "record_progress" in packet["hidden_forms"]
    assert packet["omitted_forms"][:2] == ["confirm_law", "record_progress"]
    assert packet["packet_limit"]["max_forms"] == 5
    assert len(packet["forms"]) == 5
    assert [form["form_id"] for form in packet["forms"][:2]] == ["get_task_context", "start_task"]
    assert "get_task_context" in packet["next_safe_action"]
    assert "Internal diagnostics are not available" in packet["warnings"][-1]


def test_mailbox_state_packet_surfaces_compact_context_cues() -> None:
    packet = build_mailbox_state_packet(
        state="planning",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )

    cues = packet["context_cues"]
    assert cues
    assert any(cue["cue"] == "law:internal_english_contract" for cue in cues)
    assert any(cue["cue"] == "law:mcp_first_workflow_context" for cue in cues)
    assert "law:test_contour_before_live" not in {cue["cue"] for cue in cues}
    assert "law:checkpoint_after_work_slice" not in {cue["cue"] for cue in cues}
    assert all("full_text" not in cue for cue in cues)
    assert all(str(cue.get("expand_ref") or "").startswith("cue:") for cue in cues)


def test_mailbox_state_packet_surfaces_tool_independent_edit_authority() -> None:
    planning = build_mailbox_state_packet(
        state="planning",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )
    implementation = build_mailbox_state_packet(
        state="implementation",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )
    verification = build_mailbox_state_packet(
        state="verification",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )

    assert planning["edit_authority"]["status"] == "diagnosis_only"
    assert planning["edit_authority"]["editing_allowed"] is False
    assert implementation["edit_authority"]["status"] == "no_authority"
    assert implementation["edit_authority"]["editing_allowed"] is False
    assert "edit_authority" not in verification


def test_mailbox_state_packet_surfaces_compact_health_nudge() -> None:
    packet = build_mailbox_state_packet(
        state="planning",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )

    nudge = packet["health_nudge"]
    assert nudge["reason"] == "state:planning"
    assert nudge["severity"] == "P0"
    assert "authority" in nudge["check"].lower()
    assert nudge["cue"] == "adherence:authority_before_editing"
    assert str(nudge["expand_ref"]).startswith("cue:")
    assert "full_text" not in nudge


def test_mailbox_state_health_nudge_is_stage_aware() -> None:
    planning = build_mailbox_state_packet(
        state="planning",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )
    implementation = build_mailbox_state_packet(
        state="implementation",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )

    assert "authority" in planning["health_nudge"]["check"].lower()
    assert "authority" in implementation["health_nudge"]["check"].lower()


def test_verification_state_surfaces_test_contour_before_live_cue() -> None:
    packet = build_mailbox_state_packet(
        state="verification",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )

    cue_ids = {cue["cue"] for cue in packet["context_cues"]}
    assert "law:test_contour_before_live" in cue_ids
    assert all("full_text" not in cue for cue in packet["context_cues"])


def test_checkpointing_state_surfaces_post_work_checkpoint_cue() -> None:
    packet = build_mailbox_state_packet(
        state="checkpointing",
        project="sloplesscode",
        runtime_profile_id="weak_mcp_operator",
    )

    cue_ids = {cue["cue"] for cue in packet["context_cues"]}
    assert "law:checkpoint_after_work_slice" in cue_ids
    assert all("full_text" not in cue for cue in packet["context_cues"])


def test_context_cues_for_query_respect_stage_applicability_when_state_is_known() -> None:
    planning_cues = context_cues_for_query(
        query="restart external runtime after verification contour and run live smoke",
        project="sloplesscode",
        state="planning",
    )
    verification_cues = context_cues_for_query(
        query="restart external runtime after verification contour and run live smoke",
        project="sloplesscode",
        state="verification",
    )

    assert "law:test_contour_before_live" not in {cue["cue"] for cue in planning_cues}
    assert verification_cues[0]["cue"] == "law:test_contour_before_live"


def test_context_cues_for_query_use_english_canonical_triggers() -> None:
    cues = context_cues_for_query(
        query="routing language semantic adaptation learned aliases route pattern store",
        project="sloplesscode",
    )

    cue_ids = {cue["cue"] for cue in cues}
    assert "law:internal_english_contract" in cue_ids
    assert "tool:semantic_adaptation" in cue_ids
    assert all("full_text" not in cue for cue in cues)


def test_context_cues_for_query_remind_mcp_first_workflow_context() -> None:
    cues = context_cues_for_query(
        query="use MCP public surface instead of direct API or python script for project memory and task lifecycle",
        project="sloplesscode",
    )

    assert cues[0]["cue"] == "law:mcp_first_workflow_context"
    assert cues[0]["severity"] == "P0"
    assert "full_text" not in cues[0]


def test_cognitive_health_packet_is_compact_read_only_and_project_agnostic() -> None:
    packet = build_cognitive_health_packet(
        project="alpha",
        state="implementation",
        query="agent context recall health",
        limit=5,
    )

    assert packet["status"] == "needs_self_check"
    assert packet["read_only"] is True
    assert packet["evaluator_executed"] is False
    assert packet["checks"]
    assert packet["context_cues"]
    assert all("full_text" not in check for check in packet["checks"])
    serialized = str(packet).casefold()
    assert "docker" not in serialized
    assert "sloplesscode" not in serialized


def test_public_diagnostic_incidents_are_compact_and_actionable() -> None:
    missing_project = attach_public_diagnostic_incident(
        receipt={
            "status": "needs_project",
            "resource_kind": "planning_advisor",
            "missing_fields": ["project"],
            "next_safe_action": "Call get again with project set.",
        },
        kind="missing_project_scope",
    )
    missing_claim = build_public_diagnostic_incident(
        kind="work_started_without_claim_or_missing_token",
        task_id="task-1",
        safe_next_action="Submit start_task before record_progress.",
        recommended_next_call={
            "tool": "submit",
            "form_id": "start_task",
            "payload": {"project": "alpha", "task_id": "task-1"},
        },
    )

    assert missing_project["diagnostic_incident"]["kind"] == "missing_project_scope"
    assert missing_project["diagnostic_incident"]["missing_fields"] == ["project"]
    assert missing_project["diagnostic_incident"]["safe_next_action"] == "Call get again with project set."
    assert "route_telemetry" not in missing_project["diagnostic_incident"]
    assert missing_claim["kind"] == "work_started_without_claim_or_missing_token"
    assert missing_claim["recommended_next_call"]["form_id"] == "start_task"
    assert "secret-token-value" not in str(missing_claim)


def test_context_cues_for_query_remind_test_contour_before_live_runtime() -> None:
    cues = context_cues_for_query(
        query="restart external runtime after verification contour and run live smoke",
        project="sloplesscode",
    )

    assert cues[0]["cue"] == "law:test_contour_before_live"
    assert cues[0]["severity"] == "P0"
    assert "full_text" not in cues[0]


def test_live_runtime_preflight_spec_stays_universal_and_compact() -> None:
    spec = load_named_json_spec("workflow/live_runtime_preflight.json")
    text = json.dumps(spec, ensure_ascii=False)

    assert spec["resource_kind"] == "live_runtime_preflight"
    assert any(item["id"] == "project_policy" for item in spec["checks"])
    assert "Docker" not in text
    assert "memory-server-dev" not in text
    assert "120 seconds" not in text


def test_boundary_action_cues_spec_stays_project_agnostic() -> None:
    spec = load_named_json_spec("workflow/boundary_action_cues.json")
    text = json.dumps(spec, ensure_ascii=False).casefold()

    assert "external_publication" in spec["action_classes"]
    assert "release_boundary" in spec["action_classes"]
    assert "dockerhub" not in text
    assert "docker push" not in text
    assert "publish_docker_image" not in text


def test_context_cues_for_query_remind_post_commit_checkpoint() -> None:
    cues = context_cues_for_query(
        query="commit finished, record progress checkpoint with changed files and verification",
        project="sloplesscode",
    )

    assert cues[0]["cue"] == "law:checkpoint_after_work_slice"
    assert cues[0]["severity"] == "P0"
    assert "full_text" not in cues[0]


def test_context_cues_surface_generic_adherence_without_project_runtime_details() -> None:
    cues = context_cues_for_query(
        query="finish task after mutation but I forgot claim ownership token",
        project="sloplesscode",
        state="implementation",
    )

    cue_ids = {cue["cue"] for cue in cues}
    assert "adherence:claim_before_mutation" in cue_ids
    claim = next(cue for cue in cues if cue["cue"] == "adherence:claim_before_mutation")
    assert claim["authority_layer"] == "canonical_principle"
    assert claim["source"] == "adherence_spec"
    assert "full_text" not in claim
    assert "Docker" not in str(claim)


def test_context_cues_put_edit_authority_before_implementation_initiative() -> None:
    cues = context_cues_for_query(
        query="continue implementation and edit after a new hypothesis changed the solution direction",
        project="sloplesscode",
        state="implementation",
    )

    assert cues[0]["cue"] == "adherence:authority_before_editing"
    assert cues[0]["severity"] == "P0"
    assert "full_text" not in cues[0]


def test_context_cues_surface_practical_validation_as_complementary_stage() -> None:
    cues = context_cues_for_query(
        query="agent ux practical validation should inspect receipt and next safe action through product surface",
        project="sloplesscode",
        state="verification",
    )

    cue_ids = {cue["cue"] for cue in cues}
    assert "adherence:product_surface_practical_validation" in cue_ids
    practical = next(cue for cue in cues if cue["cue"] == "adherence:product_surface_practical_validation")
    assert practical["severity"] == "P1"
    assert "full_text" not in practical


def test_context_cues_can_include_governed_canonical_laws_before_bootstrap_cues() -> None:
    cues = context_cues_for_state(
        state="planning",
        project="sloplesscode",
        governed_laws=[
            {
                "id": "law-meta-1",
                "project": "sloplesscode",
                "scope": "meta",
                "status": "active",
                "title": "Fix causes, not symptoms",
                "statement": "Fix the general mechanism behind the observed incident.",
                "rationale": "Root-cause fixes prevent repeated failures.",
                "tags": ["canonical", "root-cause"],
            }
        ],
    )

    assert cues[0]["cue"] == "law:sloplesscode:law-meta-1"
    assert cues[0]["authority_layer"] == "canonical_principle"
    assert cues[0]["source"] == "governed_law_db"
    assert cues[0]["expand_ref"] == "law:sloplesscode:law-meta-1"
    assert "full_text" not in cues[0]


def test_context_cues_distinguish_project_laws_from_canonical_principles() -> None:
    cues = context_cues_for_query(
        query="project agnostic product scope root cause",
        project="sloplesscode",
        governed_laws=[
            {
                "id": "law-project-1",
                "project": "sloplesscode",
                "scope": "project",
                "status": "active",
                "title": "System serves arbitrary governed projects",
                "statement": "SloplessCode helps arbitrary governed projects, not only itself.",
                "rationale": "Avoid project-local overfitting.",
                "tags": ["product-scope"],
            },
            {
                "id": "law-meta-1",
                "project": "sloplesscode",
                "scope": "meta",
                "status": "active",
                "title": "Solve the general class through the particular case",
                "statement": "Use particular examples to validate the general rule.",
                "rationale": "Avoid hardcoding individual cases.",
                "tags": ["canonical", "root-cause"],
            },
        ],
    )

    layers = {cue["cue"]: cue.get("authority_layer") for cue in cues}
    assert layers["law:sloplesscode:law-project-1"] == "project_rule"
    assert layers["law:sloplesscode:law-meta-1"] == "canonical_principle"


async def test_context_cue_ref_expands_full_text() -> None:
    async def unused_get(_api_base: str, _path: str):
        raise AssertionError("cue refs should resolve from the cue registry")

    async def unused_context(_api_base: str, _args: dict):
        raise AssertionError("cue refs should not request task context")

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={"project": "sloplesscode", "ref": "cue:law:internal_english_contract"},
        dependencies=PublicRefDependencies(
            get=unused_get,
            get_task_context=unused_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet is not None
    assert packet["receipt"]["resource_kind"] == "cue"
    assert packet["result"]["cue"] == "law:internal_english_contract"
    assert packet["result"]["expanded_by"] == "explicit_ref"
    assert packet["result"]["stage_applicability"]["state_known"] is False
    assert "Do not hardcode user-language phrases" in packet["result"]["full_text"]


async def test_context_cue_ref_reports_stage_applicability_when_state_is_known() -> None:
    async def unused_get(_api_base: str, _path: str):
        raise AssertionError("cue refs should resolve from the cue registry")

    async def unused_context(_api_base: str, _args: dict):
        raise AssertionError("cue refs should not request task context")

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={"project": "sloplesscode", "ref": "cue:law:test_contour_before_live", "state": "planning"},
        dependencies=PublicRefDependencies(
            get=unused_get,
            get_task_context=unused_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet is not None
    assert packet["result"]["stage_applicability"]["state"] == "planning"
    assert packet["result"]["stage_applicability"]["allowed_in_state"] is False
    assert "reference-only" in packet["receipt"]["next_safe_action"]


async def test_adherence_cue_ref_expands_full_text() -> None:
    async def unused_get(_api_base: str, _path: str):
        raise AssertionError("cue refs should resolve from the cue registry")

    async def unused_context(_api_base: str, _args: dict):
        raise AssertionError("cue refs should not request task context")

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={
            "project": "sloplesscode",
            "ref": "cue:adherence:product_surface_practical_validation",
            "state": "verification",
        },
        dependencies=PublicRefDependencies(
            get=unused_get,
            get_task_context=unused_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet is not None
    assert packet["receipt"]["resource_kind"] == "cue"
    assert packet["result"]["cue"] == "adherence:product_surface_practical_validation"
    assert packet["result"]["stage_applicability"]["allowed_in_state"] is True
    assert "complementary stage" in packet["result"]["full_text"]


async def test_law_ref_reports_explicit_expansion_metadata() -> None:
    async def fake_get(_api_base: str, path: str):
        assert path == "/laws/law-1"
        return {
            "law_id": "law-1",
            "title": "Use public MCP",
            "body": "Use public MCP for governed project state.",
        }

    async def unused_context(_api_base: str, _args: dict):
        raise AssertionError("law refs should not request task context")

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={"project": "sloplesscode", "ref": "law:sloplesscode:law-1", "state": "implementation"},
        dependencies=PublicRefDependencies(
            get=fake_get,
            get_task_context=unused_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet is not None
    assert packet["receipt"]["resource_kind"] == "law"
    assert packet["result"]["expanded_by"] == "explicit_ref"
    assert packet["result"]["stage_applicability"]["block"] == "governed_law"
    assert packet["result"]["stage_applicability"]["state"] == "implementation"
    assert packet["result"]["stage_applicability"]["allowed_in_state"] is True


async def test_rule_candidate_ref_reports_explicit_expansion_metadata() -> None:
    async def fake_get(_api_base: str, path: str):
        assert path == "/laws/candidates/candidate-1"
        return {
            "candidate_id": "candidate-1",
            "title": "Candidate",
            "body": "Draft rule candidate.",
        }

    async def unused_context(_api_base: str, _args: dict):
        raise AssertionError("rule candidate refs should not request task context")

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={"project": "sloplesscode", "ref": "rule_candidate:sloplesscode:candidate-1", "state": "handoff"},
        dependencies=PublicRefDependencies(
            get=fake_get,
            get_task_context=unused_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet is not None
    assert packet["receipt"]["resource_kind"] == "rule_candidate"
    assert packet["result"]["expanded_by"] == "explicit_ref"
    assert packet["result"]["stage_applicability"]["block"] == "rule_candidate"
    assert packet["result"]["stage_applicability"]["allowed_in_state"] is False


async def test_public_ref_not_found_receipt_includes_human_readable_diagnostic() -> None:
    async def failing_get(_api_base: str, _path: str):
        raise RuntimeError("missing")

    async def unused_context(_api_base: str, _args: dict):
        raise AssertionError("law refs should not request task context")

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={"project": "sloplesscode", "ref": "law:sloplesscode:missing-law"},
        dependencies=PublicRefDependencies(
            get=failing_get,
            get_task_context=unused_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet["receipt"]["status"] == "not_found"
    incident = packet["receipt"]["diagnostic_incident"]
    assert incident["kind"] == "public_ref_not_found"
    assert incident["resource_kind"] == "law"
    assert incident["safe_next_action"] == packet["receipt"]["next_safe_action"]
    assert "mistyped" in incident["why"]
    assert "route_telemetry" not in incident


async def test_diagnostic_public_ref_reports_non_authoritative_orphan_without_deleting_index() -> None:
    store = get_public_ref_index_store()
    store.clear()
    artifact_key = "task:sloplesscode:8f39fa86-6aa0-42e6-a8fb-2c58128207b5"
    store.upsert_artifact(
        {
            "artifact_key": artifact_key,
            "title": "Live project reconstruction bundle generator",
            "status": "open",
        }
    )

    async def failing_context(_api_base: str, _args: dict):
        raise RuntimeError("Task not found")

    async def unused_get(_api_base: str, _path: str):
        raise AssertionError("task refs use task context")

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={
            "project": "sloplesscode",
            "ref": artifact_key,
            "diagnostic": True,
            "runtime_profile_id": "diagnostic_operator",
        },
        dependencies=PublicRefDependencies(
            get=unused_get,
            get_task_context=failing_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet["receipt"]["status"] == "orphan_ref"
    assert packet["receipt"]["orphan_reference"]["artifact_key"] == artifact_key
    assert packet["receipt"]["orphan_reference"]["authoritative"] is False
    assert store.find_exact(artifact_key=artifact_key) is not None
    assert "do not restore or delete automatically" in packet["receipt"]["next_safe_action"]


async def test_ambiguous_public_ref_receipt_includes_human_readable_diagnostic(monkeypatch) -> None:
    async def unused_get(_api_base: str, _path: str):
        raise AssertionError("ambiguous short refs should stop before API lookup")

    async def unused_context(_api_base: str, _args: dict):
        raise AssertionError("ambiguous short refs should stop before task context")

    def ambiguous_resolver(**_kwargs):
        raise AmbiguousPublicRefError(
            [
                {"artifact_key": "task:sloplesscode:abcdef-1", "title": "First"},
                {"artifact_key": "task:sloplesscode:abcdef-2", "title": "Second"},
            ]
        )

    monkeypatch.setattr(mcp_simple_read_actions, "resolve_public_artifact_short_ref", ambiguous_resolver)

    packet = await build_simple_public_ref_response(
        api_base="http://test",
        args={"project": "sloplesscode", "ref": "task:sloplesscode:abcdef"},
        dependencies=PublicRefDependencies(
            get=unused_get,
            get_task_context=unused_context,
            public_error_message=lambda exc: str(exc),
        ),
    )

    assert packet["receipt"]["status"] == "ambiguous_ref"
    assert len(packet["receipt"]["matches"]) == 2
    incident = packet["receipt"]["diagnostic_incident"]
    assert incident["kind"] == "ambiguous_public_ref"
    assert incident["resource_kind"] == "task"
    assert incident["safe_next_action"] == packet["receipt"]["next_safe_action"]
    assert "must not guess" in incident["why"]


def test_law_context_block_uses_compact_refs_before_full_text() -> None:
    from datetime import datetime, timezone

    from app.models.law import ProjectLawRecord
    from app.services.law_service import build_law_context_block

    block = build_law_context_block(
        [
            ProjectLawRecord(
                id="law-1",
                project="sloplesscode",
                scope="project",
                status="active",
                title="Use MCP",
                statement="Full law statement should be available through explicit expansion.",
                rationale="Short reason for normal context packets.",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                memory_id="law-1",
            )
        ]
    )

    assert "law:sloplesscode:law-1" in block
    assert "Expand: law:sloplesscode:law-1" in block
    assert "Summary: Short reason" in block
    assert "Full law statement should be available" not in block


async def test_next_work_advisor_surfaces_improvements_when_no_tasks(monkeypatch) -> None:
    async def fake_get(_api_base: str, path: str):
        assert "/artifacts?" in path
        assert "status=open" in path
        assert "type=all" not in path
        return {
            "items": [
                {
                    "artifact_key": "improvement:sloplesscode:imp-1",
                    "type": "improvement",
                    "project": "sloplesscode",
                    "id": "imp-1",
                    "title": "Add portable export preview",
                    "status": "open",
                }
            ]
        }

    async def forbidden_post(*_args, **_kwargs):
        raise AssertionError("planning advisor should not fall through to memory search")

    async def forbidden_expert(*_args, **_kwargs):
        raise AssertionError("planning advisor should not require project expert routing")

    monkeypatch.setattr(
        planning_advisor_service,
        "build_maintenance_suggestion",
        lambda current_project, **_: {
            "status": "warning",
            "active_findings": 4,
            "top_dataset_classes": {"stale_guidance": 3},
            "scope": {
                "current_project": current_project,
                "dominant_relation": "outside_current_project",
                "notice": f"Hygiene findings mostly target other projects, not current project {current_project}.",
            },
            "why_it_matters": "Hygiene findings can pollute search and learned route selection.",
            "next_safe_action": "Review maintenance scope before promoting cleanup into project work.",
            "destructive_action_allowed": False,
            "expand_refs": ["admin:data-hygiene/workflow"],
        },
    )

    packet = await build_simple_get_query_response(
        api_base="http://test",
        args={"project": "sloplesscode", "query": "what should I do next?", "limit": 5},
        session_id=None,
        dependencies=SimpleReadDependencies(
            get=fake_get,
            post=forbidden_post,
            query_project_expert=forbidden_expert,
            extract_task_id_like=lambda _text: None,
        ),
    )

    assert packet is not None
    assert packet["receipt"]["resource_kind"] == "planning_advisor"
    assert packet["result"]["selection_rule"] == "promote_open_improvements"
    control = packet["result"]["collaborative_control"]
    assert control["framing_required"] is True
    assert control["approval_required_before_claim"] is True
    assert control["approval_intent"] == "user_approved_start"
    assert control["approval_alias_source"] == "semantic_adaptation_or_learned_aliases"
    assert "user_approved_start" in packet["result"]["next_safe_action"]
    assert packet["result"]["next_work_candidates"][0]["type"] == "improvement"
    assert packet["result"]["next_work_candidates"][0]["ref"] == "improvement:sloplesscode:imp-1"
    suggestion = packet["result"]["maintenance_suggestion"]
    assert suggestion["active_findings"] == 4
    assert suggestion["destructive_action_allowed"] is False
    assert "maintenance_suggestion" not in packet["result"]["next_work_candidates"][0]
    assert "pollute search" in suggestion["why_it_matters"]


def test_planning_advisor_collaborative_control_uses_internal_english_contracts() -> None:
    spec = load_named_json_spec("planning/advisor.json")
    text = json.dumps(spec, ensure_ascii=False)

    assert spec["collaborative_control"]["approval_intent"] == "user_approved_start"
    assert spec["collaborative_control"]["approval_alias_source"] == "semantic_adaptation_or_learned_aliases"
    assert "приступ" not in text.casefold()
    assert "начинай" not in text.casefold()


def test_task_compact_resource_includes_spec_driven_framing_gaps() -> None:
    compact = compact_resource_result(
        "task",
        {
            "project": "sloplesscode",
            "task_id": "task-1",
            "status": "ready",
            "task": {"title": "Incomplete task", "status": "planning"},
            "task_statement_quality": {
                "capture_quality": "partial",
                "missing_artifacts": ["definition_of_done"],
            },
            "next_actions": [
                {
                    "action": "Capture missing definition of done as a grounded task artifact.",
                    "rationale": "The task statement is incomplete.",
                }
            ],
        },
        tool_surface_role=lambda _tool: "public_entrypoint",
    )

    assert compact["task_framing_gaps"][0]["field"] == "definition_of_done"
    assert compact["task_framing_gaps"][0]["severity"] == "high"
    assert compact["task_framing_gaps"][0]["suggestions"]
    assert "definition of done" in compact["task_framing_gaps"][0]["recommended_action"].casefold()


def test_task_compact_resource_hides_framing_gaps_outside_planning_stage() -> None:
    compact = compact_resource_result(
        "task",
        {
            "project": "sloplesscode",
            "task_id": "task-1",
            "status": "ready",
            "task": {"title": "Task under verification", "status": "planning"},
            "task_statement_quality": {
                "capture_quality": "partial",
                "missing_artifacts": ["definition_of_done"],
            },
        },
        tool_surface_role=lambda _tool: "public_entrypoint",
        state="verification",
    )

    assert "task_framing_gaps" not in compact


def test_stage_applicability_contract_scopes_response_blocks() -> None:
    assert stage_allows_block("task_framing_gaps", state="planning") is True
    assert stage_allows_block("task_framing_gaps", state="live_validation") is False
    assert stage_allows_block("work_guidance", state="planning") is False
    assert stage_allows_block("work_guidance", state="implementation") is True
    assert stage_allows_block("edit_authority", state="implementation") is True
    assert stage_allows_block("edit_authority", state="verification") is False
    assert stage_allows_block("verification_policy", state="handoff") is False
    assert stage_allows_block("law:test_contour_before_live", state="planning") is False
    assert stage_allows_block("law:test_contour_before_live", state="verification") is True
    assert stage_allows_block("unknown_public_block", state="verification") is True


def test_mailbox_state_full_detail_bypasses_minimal_packet_limit() -> None:
    packet = build_mailbox_state_packet(
        state="planning",
        project="mnemoforge",
        runtime_profile_id="weak_mcp_operator",
        detail="full",
    )

    form_ids = [form["form_id"] for form in packet["forms"]]
    assert "record_progress" in form_ids
    assert "finish_task" in form_ids
    assert "omitted_forms" not in packet


async def test_mailbox_state_response_uses_session_runtime_profile_defaults() -> None:
    async def fake_identity_defaults(session_id: str | None) -> dict[str, str]:
        assert session_id == "sess-read"
        return {"runtime_profile_id": "weak_mcp_operator"}

    packet = await build_mailbox_state_response(
        args={"project": "mnemoforge", "state": "planning", "diagnostic": True},
        session_id="sess-read",
        dependencies=MailboxReadDependencies(get_session_identity_defaults=fake_identity_defaults),
    )

    assert packet["state"] == "planning"
    assert "_internal" not in packet
    assert "claim_task" in packet["hidden_forms"]


async def test_mailbox_state_response_suppresses_repeated_health_nudge_per_session() -> None:
    from app.services.mcp_session_store import get_session_store

    session_id = "sess-health-nudge-repeat"
    store = get_session_store()
    await store.init_session(session_id)

    async def fake_identity_defaults(session_id_arg: str | None) -> dict[str, str]:
        assert session_id_arg == session_id
        return {"runtime_profile_id": "weak_mcp_operator"}

    dependencies = MailboxReadDependencies(get_session_identity_defaults=fake_identity_defaults)
    first = await build_mailbox_state_response(
        args={"project": "mnemoforge", "state": "planning"},
        session_id=session_id,
        dependencies=dependencies,
    )
    second = await build_mailbox_state_response(
        args={"project": "mnemoforge", "state": "planning"},
        session_id=session_id,
        dependencies=dependencies,
    )
    diagnostic = await build_mailbox_state_response(
        args={"project": "mnemoforge", "state": "planning", "diagnostic": True},
        session_id=session_id,
        dependencies=dependencies,
    )

    await store.close_session(session_id)

    assert "health_nudge" in first
    assert "health_nudge" not in second
    assert "health_nudge" not in diagnostic
    assert diagnostic["health_nudge_suppressed"]["reason"] == "already_shown_in_session"


async def test_mailbox_state_response_suppresses_repeated_health_nudge_for_stateless_hosts() -> None:
    from app.services import mcp_mailbox_read

    mcp_mailbox_read._STATELESS_HEALTH_NUDGE_SEEN.clear()

    async def fake_identity_defaults(session_id_arg: str | None) -> dict[str, str]:
        assert session_id_arg is None
        return {"runtime_profile_id": "weak_mcp_operator"}

    dependencies = MailboxReadDependencies(get_session_identity_defaults=fake_identity_defaults)
    first = await build_mailbox_state_response(
        args={"project": "stateless-health-project", "state": "planning"},
        session_id=None,
        dependencies=dependencies,
    )
    second = await build_mailbox_state_response(
        args={"project": "stateless-health-project", "state": "planning", "diagnostic": True},
        session_id=None,
        dependencies=dependencies,
    )

    mcp_mailbox_read._STATELESS_HEALTH_NUDGE_SEEN.clear()

    assert "health_nudge" in first
    assert "health_nudge" not in second
    assert second["health_nudge_suppressed"]["reason"] == "stateless_cooldown"
    assert second["health_nudge_suppressed"]["cooldown_seconds"] > 0


async def test_mailbox_state_health_nudge_stateless_scope_uses_hashed_work_token() -> None:
    from app.services import mcp_mailbox_read

    mcp_mailbox_read._STATELESS_HEALTH_NUDGE_SEEN.clear()

    async def fake_identity_defaults(session_id_arg: str | None) -> dict[str, str]:
        assert session_id_arg is None
        return {"runtime_profile_id": "weak_mcp_operator"}

    dependencies = MailboxReadDependencies(get_session_identity_defaults=fake_identity_defaults)
    first = await build_mailbox_state_response(
        args={
            "project": "stateless-token-project",
            "state": "implementation",
            "work_token": "token-alpha",
        },
        session_id=None,
        dependencies=dependencies,
    )
    repeated = await build_mailbox_state_response(
        args={
            "project": "stateless-token-project",
            "state": "implementation",
            "work_token": "token-alpha",
            "diagnostic": True,
        },
        session_id=None,
        dependencies=dependencies,
    )
    different_token = await build_mailbox_state_response(
        args={
            "project": "stateless-token-project",
            "state": "implementation",
            "work_token": "token-beta",
        },
        session_id=None,
        dependencies=dependencies,
    )

    mcp_mailbox_read._STATELESS_HEALTH_NUDGE_SEEN.clear()

    assert "health_nudge" in first
    assert "health_nudge" not in repeated
    assert "health_nudge" in different_token
    repeat_key = repeated["health_nudge_suppressed"]["repeat_key"]
    assert "work_token:" in repeat_key
    assert "token-alpha" not in repeat_key


async def test_mailbox_get_response_uses_session_runtime_profile_defaults() -> None:
    async def fake_identity_defaults(session_id: str | None) -> dict[str, str]:
        assert session_id == "sess-get"
        return {"runtime_profile_id": "diagnostic_operator"}

    packet = await build_mailbox_get_response(
        args={"ref": "mailbox_state:mnemoforge:verification", "diagnostic": True},
        session_id="sess-get",
        dependencies=MailboxReadDependencies(get_session_identity_defaults=fake_identity_defaults),
    )

    assert packet["state"] == "verification"
    assert packet["_internal"]["visibility"] == "internal"
    assert all("postconditions" not in form for form in packet["forms"])


def test_mailbox_state_packet_orders_forms_by_workflow_not_filename() -> None:
    packet = build_mailbox_state_packet(
        state="planning",
        project="mnemoforge",
        runtime_profile_id="strong_mcp_operator",
    )

    form_ids = [form["form_id"] for form in packet["forms"]]
    assert form_ids[:2] == ["get_task_context", "start_task"]
    assert "record_progress" in form_ids
    assert "finish_task" in form_ids
    assert "close_task" in form_ids
    assert "store_memory" in form_ids
    assert "create_law" in form_ids
    assert "confirm_law" in form_ids
    assert form_ids.index("claim_task") > form_ids.index("start_task")
    forms = {form["form_id"]: form for form in packet["forms"]}
    assert "before any real implementation work" in forms["start_task"]["hint"]
    assert "user_approved_start" in forms["start_task"]["hint"]
    assert packet["hidden_forms"] == []


def test_checkpointing_state_prefers_finish_task_or_progress_forms() -> None:
    packet = build_mailbox_state_packet(
        state="checkpointing",
        project="mnemoforge",
        runtime_profile_id="strong_mcp_operator",
    )

    form_ids = {form["form_id"] for form in packet["forms"]}
    forms = {form["form_id"]: form for form in packet["forms"]}
    assert {"finish_task", "record_progress", "release_task_claim"} <= form_ids
    assert "commit" in forms["record_progress"]["hint"]
    assert "publish" in forms["record_progress"]["hint"]
    assert "must already have been claimed" in forms["record_progress"]["hint"]
    assert "diagnostic/operator feedback" in forms["record_progress"]["hint"]
    assert "diagnostic/operator feedback" in forms["finish_task"]["hint"]
    assert "Docker" not in forms["record_progress"]["hint"]
    assert "Docker" not in forms["finish_task"]["hint"]
    assert "finish_task" in packet["next_safe_action"]


def test_mailbox_state_packet_can_return_internal_metadata_for_diagnostic_profile() -> None:
    packet = build_mailbox_state_packet(
        state="planning",
        project="mnemoforge",
        runtime_profile_id="diagnostic_operator",
        diagnostic=True,
    )

    assert packet["state"] == "planning"
    assert "_internal" in packet
    assert packet["_internal"]["visibility"] == "internal"
    assert "response_envelope" in packet["_internal"]
    assert any(form["form_id"] == "create_improvement" for form in packet["_internal"]["forms"])


def test_mailbox_verification_packet_points_to_approved_contour_form() -> None:
    packet = build_mailbox_state_packet(
        state="verification",
        project="mnemoforge",
        runtime_profile_id="unknown_cli",
    )

    form_ids = {form["form_id"] for form in packet["forms"]}
    assert "run_verification" in form_ids
    assert "get_task_context" in form_ids
    assert "project-approved verification contour" in packet["next_safe_action"]
    assert "_internal" not in packet


def test_mailbox_submit_rejects_forms_outside_current_state() -> None:
    packet = build_mailbox_submit_receipt(
        form_id="run_verification",
        state="planning",
        project="mnemoforge",
        payload={"project": "mnemoforge", "changed_files": ["app/services/mcp_mailbox.py"]},
    )

    assert packet["receipt"]["status"] == "rejected"
    assert "not available in state planning" in packet["receipt"]["message"]
    incident = packet["receipt"]["diagnostic_incident"]
    assert incident["kind"] == "form_unavailable_in_state"
    assert incident["safe_next_action"] == packet["receipt"]["next_safe_action"]
    assert "state mismatch" in incident["why"]
    assert "help:workflow.state" in incident["expand_refs"]


def test_mailbox_submit_reports_missing_required_fields() -> None:
    packet = build_mailbox_submit_receipt(
        form_id="create_improvement",
        state="planning",
        project="mnemoforge",
        payload={"project": "mnemoforge", "title": "Mailbox FSM"},
    )

    assert packet["receipt"]["status"] == "needs_input"
    assert packet["receipt"]["missing_fields"] == ["summary", "next_step"]
    incident = packet["receipt"]["diagnostic_incident"]
    assert incident["kind"] == "missing_required_fields"
    assert incident["missing_fields"] == ["summary", "next_step"]
    assert incident["safe_next_action"] == packet["receipt"]["next_safe_action"]


def test_mailbox_submit_unknown_form_reports_public_diagnostic() -> None:
    packet = build_mailbox_submit_receipt(
        form_id="finish_everything",
        state="planning",
        project="mnemoforge",
        payload={"project": "mnemoforge"},
    )

    assert packet["receipt"]["status"] == "rejected"
    incident = packet["receipt"]["diagnostic_incident"]
    assert incident["kind"] == "unknown_mailbox_form"
    assert incident["resource_kind"] == "mailbox_form"
    assert incident["safe_next_action"] == packet["receipt"]["next_safe_action"]


def test_mailbox_submit_verification_returns_project_contour_ref_not_command() -> None:
    packet = build_mailbox_submit_receipt(
        form_id="run_verification",
        state="verification",
        project="mnemoforge",
        payload={"project": "mnemoforge", "changed_files": ["app/services/mcp_mailbox.py"]},
    )

    receipt = packet["receipt"]
    assert receipt["status"] == "ready"
    assert receipt["data_ref"] == "verification_contour:mnemoforge:verification"
    assert "approved_command" not in receipt
    assert "host execution_context" in receipt["forbidden_patterns"]
    assert "project-approved verification contour" in receipt["message"]


def test_mailbox_submit_write_form_is_guarded_until_server_mutation_exists() -> None:
    packet = build_mailbox_submit_receipt(
        form_id="create_improvement",
        state="planning",
        project="mnemoforge",
        payload={
            "project": "mnemoforge",
            "title": "Mailbox FSM",
            "summary": "Keep external MCP forms stable while internal routes migrate.",
            "next_step": "Implement governed mutation after receipt validation.",
        },
    )

    receipt = packet["receipt"]
    assert receipt["status"] == "needs_review"
    assert receipt["mode"] == "write"
    assert "no write was performed" in receipt["message"]
    assert "Ask Clerk" in receipt["next_safe_action"]


def test_mailbox_postcondition_health_detects_semantic_mismatch() -> None:
    form = mailbox_form_by_id("create_improvement")
    assert form is not None

    health = evaluate_mailbox_postconditions(
        form,
        {
            "result_kind": "checkpoint_draft_created",
            "artifact_type": "task_checkpoint",
            "mutation": True,
            "review_mode": False,
            "internal_tool": "clerk_draft_report",
        },
    )

    assert health["ok"] is False
    failure_fields = {failure["field"] for failure in health["failures"]}
    assert {"result_kind", "artifact_type", "internal_tool"} <= failure_fields


def test_set_feature_gate_form_is_public_mailbox_control() -> None:
    packet = build_mailbox_state_packet(state="planning", project="mnemoforge")

    form = next(item for item in packet["forms"] if item["form_id"] == "set_feature_gate")
    assert form["mode"] == "transition"
    assert {"feature_id", "enabled", "scope"} <= set(form["required_fields"])
    assert "postconditions" not in form


def test_route_feedback_form_is_operator_review_only() -> None:
    planning = build_mailbox_state_packet(state="planning", project="mnemoforge", detail="full")
    operator_review = build_mailbox_state_packet(state="operator_review", project="mnemoforge")

    assert not any(form["form_id"] == "route_feedback" for form in planning["forms"])
    form = next(item for item in operator_review["forms"] if item["form_id"] == "route_feedback")
    assert form["mode"] == "transition"
    assert {"facade", "reason"} <= set(form["required_fields"])
    assert {
        "vote",
        "language",
        "phrase_family",
        "jargon_terms",
        "typo_terms",
        "keyboard_layout_terms",
        "expected_payload",
    } <= set(form["optional_fields"])
    assert "postconditions" not in form


def test_route_hygiene_form_is_operator_review_only() -> None:
    planning = build_mailbox_state_packet(state="planning", project="mnemoforge", detail="full")
    operator_review = build_mailbox_state_packet(state="operator_review", project="mnemoforge")

    assert not any(form["form_id"] == "route_hygiene" for form in planning["forms"])
    form = next(item for item in operator_review["forms"] if item["form_id"] == "route_hygiene")
    assert form["mode"] == "read"
    assert form["required_fields"] == ["project"]
    assert "postconditions" not in form


def test_diagnostic_inspection_form_is_operator_review_only() -> None:
    planning = build_mailbox_state_packet(state="planning", project="mnemoforge", detail="full")
    operator_review = build_mailbox_state_packet(state="operator_review", project="mnemoforge")

    assert not any(form["form_id"] == "diagnostic_inspection" for form in planning["forms"])
    form = next(item for item in operator_review["forms"] if item["form_id"] == "diagnostic_inspection")
    assert form["mode"] == "read"
    assert form["required_fields"] == ["project"]
    assert {"target", "facade", "query", "ref", "metadata"} <= set(form["optional_fields"])
    assert "postconditions" not in form


def test_developer_feedback_packet_form_is_operator_review_only() -> None:
    planning = build_mailbox_state_packet(state="planning", project="mnemoforge", detail="full")
    operator_review = build_mailbox_state_packet(state="operator_review", project="mnemoforge")

    assert not any(form["form_id"] == "developer_feedback_packet" for form in planning["forms"])
    form = next(item for item in operator_review["forms"] if item["form_id"] == "developer_feedback_packet")
    assert form["mode"] == "read"
    assert {"project", "title", "observed_behavior", "expected_behavior"} <= set(form["required_fields"])
    assert {"diagnostic_payload", "evidence_refs", "reproduction_steps"} <= set(form["optional_fields"])
    assert "postconditions" not in form


def test_mailbox_state_internal_diagnostics_include_runtime_feature_gate_overrides() -> None:
    from app.services.mcp_feature_gates import get_mcp_feature_gate_store

    get_mcp_feature_gate_store().set_gate(
        feature_id="llm_route_fallback",
        scope="project",
        scope_id="mnemoforge",
        enabled=False,
        reason="test project quarantine",
        updated_by="test",
    )

    packet = build_mailbox_state_packet(
        state="planning",
        project="mnemoforge",
        runtime_profile_id="diagnostic_operator",
        diagnostic=True,
    )

    assert "llm_route_fallback" in packet["_internal"]["disabled_features"]


def test_mailbox_get_fetches_public_state_packet_by_reference() -> None:
    packet = build_mailbox_get_packet(
        ref="mailbox_state:mnemoforge:verification",
        runtime_profile_id="weak_mcp_operator",
        diagnostic=True,
    )

    assert packet["state"] == "verification"
    assert packet["project"] == "mnemoforge"
    assert "_internal" not in packet
    assert any(form["form_id"] == "run_verification" for form in packet["forms"])


def test_mailbox_get_unknown_reference_returns_public_not_found_receipt() -> None:
    packet = build_mailbox_get_packet(
        ref="internal_route:record_work_result",
        state="planning",
        project="mnemoforge",
    )

    assert packet["receipt"]["status"] == "not_found"
    assert "_internal" not in packet
