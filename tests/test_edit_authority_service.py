from app.services.edit_authority_service import build_edit_authority


def test_planning_is_diagnosis_only() -> None:
    authority = build_edit_authority(state="planning")

    assert authority["status"] == "diagnosis_only"
    assert authority["editing_allowed"] is False
    assert authority["severity"] == "P0"
    assert "NOT AUTHORIZED" in authority["instruction"]
    assert authority["tool_independent"] is True


def test_implementation_state_alone_does_not_grant_authority() -> None:
    authority = build_edit_authority(state="implementation")

    assert authority["status"] == "no_authority"
    assert authority["editing_allowed"] is False
    assert "WORKFLOW STATE ALONE" in authority["instruction"]


def test_latest_explicitly_approved_framing_grants_bounded_authority() -> None:
    authority = build_edit_authority(
        state="implementation",
        task_id="task-1",
        approved_framing="Fix the approved receipt defect without changing storage.",
        approval_intent="user_approved_start",
    )

    assert authority["status"] == "approved_implementation"
    assert authority["editing_allowed"] is True
    assert authority["framing_version"].startswith("framing:")
    assert authority["approved_framing"].startswith("Fix the approved")
    assert authority["approval_applies_only_to_latest_framing"] is True
    assert "continue" in authority["generic_continuation_is_not_approval"]


def test_scope_drift_revokes_existing_approval_and_emits_incident() -> None:
    authority = build_edit_authority(
        state="implementation",
        task_id="task-1",
        approved_framing="Fix the approved receipt defect.",
        approval_intent="user_approved_start",
        drift_dimensions=["defect_cause", "solution_direction"],
    )

    assert authority["status"] == "scope_drift_stop"
    assert authority["editing_allowed"] is False
    assert authority["scope_drift"]["reported_dimensions"] == ["defect_cause", "solution_direction"]
    assert authority["adherence_incident"]["kind"] == "edit_authority_scope_drift"
    assert "revised framing" in authority["next_safe_action"].lower()


def test_ambiguity_is_scope_drift() -> None:
    authority = build_edit_authority(
        state="implementation",
        approved_framing="Implement the approved bounded change.",
        approval_intent="user_approved_start",
        ambiguous=True,
    )

    assert authority["status"] == "scope_drift_stop"
    assert authority["editing_allowed"] is False


def test_valid_autonomous_mode_grants_bounded_authority_without_generic_continuation() -> None:
    authority = build_edit_authority(
        state="implementation",
        task_id="task-1",
        framing_version="v1",
        autonomous_mode={
            "mode": "explicit_autonomous_mode",
            "authority_granted": True,
            "approved_task_ids": ["task-1"],
        },
    )

    assert authority["status"] == "approved_implementation"
    assert authority["editing_allowed"] is True
    assert authority["authority_source"] == "explicit_autonomous_mode"
    assert authority["framing_version"] == "v1"
