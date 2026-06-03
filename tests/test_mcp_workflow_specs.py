from __future__ import annotations

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
from app.services.mcp_simple_read_actions import (
    PublicRefDependencies,
    SimpleReadDependencies,
    build_simple_get_query_response,
    build_simple_public_ref_response,
)
from app.services.mcp_simple_surface_actions import compact_resource_result
from app.services.context_cue_service import context_cues_for_query
from app.services.mcp_workflow_specs import (
    list_mailbox_forms_for_state,
    load_clerk_capture_registry,
    load_feature_toggle_registry,
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
    } <= set(summary["mailbox_forms"])


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
    assert all("full_text" not in cue for cue in cues)
    assert all(str(cue.get("expand_ref") or "").startswith("cue:") for cue in cues)


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


def test_context_cues_for_query_use_english_canonical_triggers() -> None:
    cues = context_cues_for_query(
        query="routing language semantic adaptation learned aliases route pattern store",
        project="sloplesscode",
    )

    cue_ids = {cue["cue"] for cue in cues}
    assert "law:internal_english_contract" in cue_ids
    assert "tool:semantic_adaptation" in cue_ids
    assert all("full_text" not in cue for cue in cues)


def test_context_cues_for_query_remind_test_contour_before_live_runtime() -> None:
    cues = context_cues_for_query(
        query="restart external runtime after verification contour and run live smoke",
        project="sloplesscode",
    )

    assert cues[0]["cue"] == "law:test_contour_before_live"
    assert cues[0]["severity"] == "P0"
    assert "full_text" not in cues[0]


def test_context_cues_for_query_remind_post_commit_checkpoint() -> None:
    cues = context_cues_for_query(
        query="commit finished, record progress checkpoint with changed files and verification",
        project="sloplesscode",
    )

    assert cues[0]["cue"] == "law:checkpoint_after_work_slice"
    assert cues[0]["severity"] == "P0"
    assert "full_text" not in cues[0]


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
    assert "Do not hardcode user-language phrases" in packet["result"]["full_text"]


async def test_next_work_advisor_surfaces_improvements_when_no_tasks() -> None:
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
    assert packet["result"]["next_work_candidates"][0]["type"] == "improvement"
    assert packet["result"]["next_work_candidates"][0]["ref"] == "improvement:sloplesscode:imp-1"


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


def test_mailbox_submit_reports_missing_required_fields() -> None:
    packet = build_mailbox_submit_receipt(
        form_id="create_improvement",
        state="planning",
        project="mnemoforge",
        payload={"project": "mnemoforge", "title": "Mailbox FSM"},
    )

    assert packet["receipt"]["status"] == "needs_input"
    assert packet["receipt"]["missing_fields"] == ["summary", "next_step"]


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
