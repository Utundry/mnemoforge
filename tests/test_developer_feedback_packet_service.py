from app.services.developer_feedback_packet_service import build_developer_feedback_packet


def test_developer_feedback_packet_is_read_only_and_excluded_from_learning(monkeypatch):
    from app.services import developer_feedback_packet_service as service

    def fake_diagnostic_packet(*, project: str, payload: dict, diagnostic: bool = False):
        assert project == "alpha"
        assert payload["target"] == "routing"
        return {
            "status": "ok",
            "target": "routing",
            "read_only": True,
            "summary": {"sections": ["routing"], "findings": 1, "likely_source": "route_pattern_store"},
            "findings": [{"section": "routing", "type": "unknown_tool", "severity": "high"}],
            "next_diagnostic_action": "Use route_feedback only after confirming a concrete misroute.",
        }

    monkeypatch.setattr(service, "build_diagnostic_inspection_packet", fake_diagnostic_packet)

    packet = build_developer_feedback_packet(
        project="alpha",
        payload={
            "title": "Completed task query misroutes",
            "area": "routing",
            "severity": "high",
            "observed_behavior": "A completed-task query returned open tasks.",
            "expected_behavior": "The query should return done task artifacts.",
            "reproduction_steps": ["Ask for latest implemented task."],
            "evidence_refs": ["task:alpha:task-1"],
            "query": "find latest implemented task",
        },
        diagnostic=True,
    )

    assert packet["status"] == "ready"
    assert packet["read_only"] is True
    assert packet["auto_submitted"] is False
    assert packet["learning_guardrail"]["eligible"] is False
    assert packet["diagnostic_summary"]["summary"]["likely_source"] == "route_pattern_store"
    assert "Project: alpha" in packet["developer_summary"]


def test_developer_feedback_packet_reports_missing_required_story_fields():
    packet = build_developer_feedback_packet(
        project="alpha",
        payload={
            "title": "Incomplete issue report",
            "area": "other",
            "include_diagnostic": False,
        },
    )

    assert packet["status"] == "needs_input"
    assert packet["missing_fields"] == ["observed_behavior", "expected_behavior"]
    assert packet["next_safe_action"] == "Fill the missing fields before sending this packet to developers."
