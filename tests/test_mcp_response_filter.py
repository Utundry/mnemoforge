from app.services.mcp_response_filter import filter_mcp_response, response_profile_from_args


def test_compact_profile_removes_diagnostics_and_keeps_continuation_handles():
    packet = {
        "receipt": {
            "status": "accepted",
            "message": "Task started.",
            "work_token": "secret-token",
            "lease": {"lease_id": "lease-1"},
            "work_session": {"work_id": "work-1"},
        },
        "result": {"task_id": "task-1", "title": "Continue safely."},
        "route_telemetry": {"confidence": 0.91},
        "semantic_rules": {"applied_rule_count": 2},
        "simple_interface": {"tool": "submit"},
        "warnings": [],
        "stage": "testing",
        "feedback_expected": True,
        "follow_up": "tool_feedback",
    }

    compact = filter_mcp_response(packet, profile="compact")

    assert compact["receipt"]["work_token"] == "secret-token"
    assert compact["receipt"]["lease"]["lease_id"] == "lease-1"
    assert compact["receipt"]["work_session"]["work_id"] == "work-1"
    assert compact["result"]["title"] == "Continue safely."
    assert "route_telemetry" not in compact
    assert "semantic_rules" not in compact
    assert "simple_interface" not in compact
    assert "warnings" not in compact
    assert "stage" not in compact
    assert "feedback_expected" not in compact
    assert "follow_up" not in compact


def test_diagnostic_profile_keeps_diagnostics():
    packet = {
        "receipt": {"status": "accepted"},
        "route_telemetry": {"confidence": 0.91},
        "simple_interface": {"tool": "get"},
        "server_build": {"git_sha": "abc123"},
        "stage": "testing",
        "feedback_expected": True,
    }

    diagnostic = filter_mcp_response(packet, profile="diagnostic")

    assert diagnostic["route_telemetry"]["confidence"] == 0.91
    assert diagnostic["simple_interface"]["tool"] == "get"
    assert diagnostic["server_build"]["git_sha"] == "abc123"
    assert diagnostic["stage"] == "testing"
    assert diagnostic["feedback_expected"] is True


def test_unknown_fields_fallback_to_public_visibility_for_old_packets():
    packet = {
        "legacy_status": "ok",
        "legacy_nested": {"new_field": "still visible"},
        "route_telemetry": {"confidence": 0.5},
    }
    spec = {
        "default_visibility": "public",
        "profiles": {
            "compact": {
                "include_visibility": ["public", "continuation"],
                "drop_empty": True,
            }
        },
        "field_visibility": {"route_telemetry": "diagnostic"},
        "path_visibility": {},
    }

    compact = filter_mcp_response(packet, profile="compact", spec=spec)

    assert compact == {
        "legacy_status": "ok",
        "legacy_nested": {"new_field": "still visible"},
    }


def test_response_profile_from_args_prefers_diagnostic_then_full_then_compact():
    assert response_profile_from_args({"diagnostic": True, "detail": "full"}) == "diagnostic"
    assert response_profile_from_args({"response_format": "diagnostic"}) == "diagnostic"
    assert response_profile_from_args({"detail": "full"}) == "full"
    assert response_profile_from_args({}) == "compact"
