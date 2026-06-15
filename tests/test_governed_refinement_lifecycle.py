from app.services.governed_refinement_lifecycle import (
    build_refinement_lifecycle,
    complete_refinement_lifecycle,
)
from app.services.mcp_workflow_specs import load_named_json_spec


def test_governed_refinement_lifecycle_is_target_agnostic() -> None:
    lifecycle = build_refinement_lifecycle(
        project="alpha",
        payload={
            "reason": "The selected artifact does not match the requested work.",
            "observed_behavior": "An unrelated artifact was selected.",
            "expected_behavior": "The requested artifact should be selected.",
            "provenance": "diagnostic_inspection",
            "evidence_refs": ["diagnostic:artifact:1"],
            "confidence": 0.91,
            "proposed_refinement": "Quarantine the contaminated selector.",
            "apply": True,
        },
        target_ref="artifact:alpha:item-1",
        target_type="artifact",
        action="quarantine",
        actor="operator",
        adapter="artifact_feedback",
    )

    completed = complete_refinement_lifecycle(
        lifecycle,
        status="applied",
        mutation_executed=True,
        postcondition_expected={"active": False},
        postcondition_actual={"active": False},
        postcondition_satisfied=True,
        audit_evidence=["audit:event:1"],
        reversible=True,
        reversal_action="reopen",
    )

    assert completed["contract"] == "governed_refinement_lifecycle"
    assert completed["target"]["type"] == "artifact"
    assert completed["proposal"]["action"] == "quarantine"
    assert completed["postcondition"]["satisfied"] is True
    assert completed["audit"]["evidence_refs"] == ["diagnostic:artifact:1", "audit:event:1"]
    assert completed["audit"]["reversal_action"] == "reopen"
    assert all(item["status"] == "complete" for item in completed["phases"])


def test_existing_diagnostics_and_policy_are_declared_lifecycle_adapters() -> None:
    spec = load_named_json_spec("governance/refinement_lifecycle.json")

    assert spec["adapters"]["diagnostic_inspection"] == {
        "role": "observation",
        "mutating": False,
    }
    assert spec["adapters"]["route_hygiene"]["role"] == "diagnosis"
    assert spec["adapters"]["learning_eligibility"]["role"] == "authority_gate"
    assert spec["adapters"]["route_pattern_feedback"]["role"] == "review_apply_verify"
