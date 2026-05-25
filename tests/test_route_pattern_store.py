from app.services.route_pattern_store import RoutePatternStore


def test_route_pattern_store_reuses_exact_pattern(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")

    pattern_id = store.record(
        facade="project_context",
        pattern="can this repo be used yet?",
        intent_type="project_readiness",
        tool="get_project_readiness",
        confidence=0.91,
        metadata={"matched_example": "check project readiness"},
    )

    match = store.match(
        facade="project_context",
        pattern="can this repo be used yet?",
        allowed_intent_types={"project_readiness"},
    )

    assert match is not None
    assert match["pattern_id"] == pattern_id
    assert match["backend_used"] == "learned_exact"
    assert match["intent_type"] == "project_readiness"
    assert match["tool"] == "get_project_readiness"


def test_route_pattern_store_reuses_semantic_pattern(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")
    store.record(
        facade="project_context",
        pattern="fresh agent recovery packet",
        intent_type="reconstruction_bundle",
        tool="get_project_reconstruction_bundle",
        confidence=0.88,
    )

    match = store.match(
        facade="project_context",
        pattern="fresh agent recovery bundle",
        allowed_intent_types={"reconstruction_bundle"},
    )

    assert match is not None
    assert match["backend_used"] == "learned_semantic"
    assert match["matched_by"] == "semantic"
    assert match["intent_type"] == "reconstruction_bundle"


def test_route_pattern_store_masks_uuid_values_for_reuse(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")
    store.record(
        facade="project_context",
        pattern="details for task 382e7306-cb61-46ee-8398-bc0a9bdfd9ef",
        intent_type="task_details",
        tool="pull_task_context",
        confidence=0.95,
    )

    match = store.match(
        facade="project_context",
        pattern="details for task 50b5c81a-0000-4000-9000-000000000000",
        allowed_intent_types={"task_details"},
    )

    assert match is not None
    assert match["backend_used"] == "learned_exact"
    assert match["intent_type"] == "task_details"


def test_route_pattern_store_disable_pattern_removes_it_from_matching(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")
    pattern_id = store.record(
        facade="project_rules",
        pattern="propose new law",
        intent_type="list_candidates",
        tool="list_rule_candidates",
        confidence=0.99,
    )

    assert store.match(
        facade="project_rules",
        pattern="propose new law",
        allowed_intent_types={"list_candidates", "propose_law"},
    )

    assert store.disable_pattern(
        pattern_id,
        reason="conflicts_with_structural_route",
        metadata={"expected_tool": "create_project_law", "learned_tool": "list_rule_candidates"},
    )

    assert store.match(
        facade="project_rules",
        pattern="propose new law",
        allowed_intent_types={"list_candidates", "propose_law"},
    ) is None


def test_route_pattern_store_records_positive_feedback_for_user_phrasing(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")
    pattern_id = store.record(
        facade="project_work",
        pattern="podnimi aktivnye zadachi",
        intent_type="next_priority",
        tool="list_open_tasks",
        confidence=0.7,
        metadata={"matched_example": "list active tasks"},
    )

    feedback = store.record_feedback(
        pattern_id,
        vote="positive",
        reason="Useful user-language phrase.",
        metadata={"language": "ru-latn", "phrase_family": "list active work"},
    )

    assert feedback is not None
    assert feedback["positive_feedback"] == 1
    assert feedback["negative_feedback"] == 0
    assert feedback["evidence_count"] == 2
    assert feedback["confidence"] > 0.7
    assert store.match(
        facade="project_work",
        pattern="podnimi aktivnye zadachi",
        allowed_intent_types={"next_priority"},
    )


def test_route_pattern_store_hygiene_report_flags_unknown_tools(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")
    pattern_id = store.record(
        facade="ask_project",
        pattern="find memory with usability report",
        intent_type="task_review",
        tool="missing_memory_store",
        confidence=0.9,
    )
    store.record_feedback(pattern_id, vote="negative", reason="Misrouted read request.")
    store.record_feedback(pattern_id, vote="negative", reason="Still misrouted.")

    report = store.hygiene_report(known_tools={"memory_search"}, limit=20)

    finding_types = {item["type"] for item in report["findings"]}
    assert "unknown_tool" in finding_types
    assert "negative_feedback" in finding_types
    assert report["summary"]["active_patterns"] == 1
