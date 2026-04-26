from pathlib import Path

from app.services import operational_instincts_service as service


def test_active_operational_instincts_include_onboarding_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="onboarding", limit=10)

    ids = {item["instinct_id"] for item in items}
    assert "trust_first" in ids
    assert "project_scope_first" in ids
    assert "system_must_explain_itself" in ids


def test_project_local_override_wins_over_builtin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    service.upsert_operational_instinct(
        instinct_id="project_scope_first",
        layer="project_local",
        scope_ref="alpha",
        rank="P0",
        scope="project",
        trigger="Any project work.",
        action="Use project-local override action.",
        why_it_matters="Project override should win.",
        failure_if_missing="Wrong discipline.",
        activation_tags=["task_enrichment", "project"],
    )

    items = service.get_active_operational_instincts(
        context_type="task_enrichment",
        project_id="alpha",
        limit=10,
    )
    instinct = next(item for item in items if item["instinct_id"] == "project_scope_first")
    assert instinct["layer"] == "project_local"
    assert instinct["action"] == "Use project-local override action."


def test_task_enrichment_can_activate_sparse_knowledge_instinct(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(
        context_type="task_enrichment",
        project_id="alpha",
        code_inspection_recommended=True,
        limit=10,
    )

    ids = {item["instinct_id"] for item in items}
    assert "ask_memory_before_code" in ids
    assert "raw_is_not_knowledge" in ids
    assert "unified_mcp_surface_first" in ids


def test_task_enrichment_includes_unified_mcp_surface_instinct(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(
        context_type="task_enrichment",
        project_id="alpha",
        limit=10,
    )

    ids = {item["instinct_id"] for item in items}
    assert "unified_mcp_surface_first" in ids


def test_unified_mcp_surface_instinct_mentions_list_artifacts_and_no_sql(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(
        context_type="task_enrichment",
        project_id="alpha",
        limit=20,
    )
    instinct = next(item for item in items if item["instinct_id"] == "unified_mcp_surface_first")

    assert "list_artifacts" in instinct["action"]
    assert "do not read project tables directly" in instinct["action"].lower()
    assert "report the outcome back to SuperMemory" in instinct["action"]


def test_render_operational_instincts_block_is_human_and_llm_readable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="onboarding", limit=2)
    block = service.render_operational_instincts_block(items)

    assert "## Active Operational Instincts" in block
    assert "Action:" in block
    assert "Why:" in block


def test_task_lifecycle_phase_instincts_can_be_selected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="task_framing", limit=20)

    ids = {item["instinct_id"] for item in items}
    assert "every_task_must_exist_in_memory" in ids
    assert "assume_initial_task_statement_is_incomplete" in ids
    assert "clarify_scope_assumptions_and_done" in ids
    assert "calibrate_dialogue_depth_to_user" in ids
    assert "track_assumptions_explicitly" in ids
    assert any(item["phase"] == "task_framing" for item in items)
    assert all(item["family"] == "task_lifecycle" for item in items)


def test_option_selection_includes_cost_to_value_instincts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="option_selection", limit=20)

    ids = {item["instinct_id"] for item in items}
    assert "rank_options_before_committing" in ids
    assert "cost_to_value_first" in ids
    assert "escalate_capability_cost_only_when_roi_is_positive" in ids
    assert any(item["phase"] == "option_selection" for item in items)


def test_list_operational_instincts_can_filter_by_family(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.list_operational_instincts(family="task_lifecycle")

    assert items
    assert all(item["family"] == "task_lifecycle" for item in items)


def test_list_operational_instincts_can_filter_by_phase(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.list_operational_instincts(phase="post_validation")

    assert items
    assert all(item["phase"] == "post_validation" for item in items)


def test_activation_summary_tracks_contexts_and_phases(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    service.get_active_operational_instincts(context_type="task_framing", limit=10)
    service.get_active_operational_instincts(context_type="post_validation", limit=10)

    summary = service.build_operational_instinct_activation_summary(limit=20)

    assert summary["recent_event_count"] >= 2
    assert summary["by_context"]["task_framing"] >= 1
    assert summary["by_phase"]["task_framing"] >= 1
    assert summary["by_phase"]["post_validation"] >= 1


def test_task_lifecycle_playbook_is_phase_ordered(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    playbook = service.build_operational_instinct_playbook(family="task_lifecycle")

    assert playbook["phase_sequence"][:3] == ["idea_capture", "task_framing", "option_selection"]
    assert playbook["phase_count"] >= 6
    task_framing = next(item for item in playbook["phases"] if item["phase"] == "task_framing")
    assert task_framing["objective"]
    assert "every_task_must_exist_in_memory" in task_framing["instinct_ids"]
    assert "every_task_must_exist_in_memory" in task_framing["core_instinct_ids"]
    assert "calibrate_dialogue_depth_to_user" in task_framing["supporting_instinct_ids"]
    assert task_framing["priority_summary"]["P0"] >= 1


def test_pre_implementation_includes_small_proving_and_reversible_steps(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="pre_implementation", limit=20)

    ids = {item["instinct_id"] for item in items}
    assert "implement_iteratively" in ids
    assert "define_done_for_this_iteration" in ids
    assert "prefer_smallest_proving_step" in ids
    assert "use_reversible_first_steps" in ids
    assert "bounded_ownership_before_parallel_split" in ids
    assert "keep_parallel_packets_narrow" in ids
    assert "parallelize_only_with_mergeable_packets" in ids


def test_post_implementation_has_intermediate_scope_and_regression_check(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="post_implementation", limit=20)

    ids = {item["instinct_id"] for item in items}
    assert "check_scope_and_regressions_before_full_validation" in ids
    assert any(item["phase"] == "post_implementation" for item in items)


def test_post_validation_includes_real_roi_validation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="post_validation", limit=20)

    ids = {item["instinct_id"] for item in items}
    assert "validate_beyond_synthetic_tests" in ids
    assert "validate_capability_layers_against_real_roi" in ids
    assert "record_rejected_paths" in ids
    assert "close_with_outcome_and_followups" in ids
    assert "verify_then_close_packets" in ids
    assert "record_merge_back_trace" in ids


def test_general_instincts_include_queryable_packet_lifecycle(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_DB_PATH", tmp_path / "operational_instincts.db")
    items = service.get_active_operational_instincts(context_type="onboarding", limit=20)

    ids = {item["instinct_id"] for item in items}
    assert "packet_lifecycle_must_remain_queryable" in ids
