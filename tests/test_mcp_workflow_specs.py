from __future__ import annotations

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
    load_tool_family_registry,
    load_tool_surface_spec,
    validate_specs,
)


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


def test_verification_state_blocks_host_pytest() -> None:
    spec = load_state_spec("verification")

    host_pytest = next(pattern for pattern in spec.forbidden_patterns if pattern.id == "host_pytest")
    assert "pytest" in host_pytest.match
    assert "Docker test contour" in host_pytest.message


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
    assert "pytest" in run_verification.postconditions.forbidden_metadata["command"]


def test_mailbox_form_policy_is_declarative_priority_and_visibility_source() -> None:
    policy = load_mailbox_form_policy_spec()
    minimal_rule = next(rule for rule in policy.visibility_rules if rule.packet_profile == "minimal")

    assert policy.state_priorities["planning"][:2] == ["get_task_context", "start_task"]
    assert policy.state_priorities["planning"].index("claim_task") > policy.state_priorities["planning"].index("start_task")
    assert minimal_rule.hidden_form_ids == ["claim_task"]
    assert minimal_rule.hide_only_when_form_ids_available == ["start_task"]


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
    assert [form["form_id"] for form in packet["forms"][:2]] == ["get_task_context", "start_task"]
    assert "get_task_context" in packet["next_safe_action"]
    assert "Internal diagnostics are not available" in packet["warnings"][-1]


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
    assert {"finish_task", "record_progress", "release_task_claim"} <= form_ids
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


def test_mailbox_submit_verification_returns_docker_contour_not_host_pytest() -> None:
    packet = build_mailbox_submit_receipt(
        form_id="run_verification",
        state="verification",
        project="mnemoforge",
        payload={"project": "mnemoforge", "changed_files": ["app/services/mcp_mailbox.py"]},
    )

    receipt = packet["receipt"]
    assert receipt["status"] == "ready"
    assert receipt["approved_command"].startswith("./scripts/run_pytest_docker.ps1 -NoBuild")
    assert "python -m pytest" in receipt["forbidden_patterns"]
    assert "Host pytest is forbidden" in receipt["message"]


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
