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


def test_route_pattern_store_preview_match_does_not_record_hit(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")
    pattern_id = store.record(
        facade="project_context",
        pattern="find recent implemented tasks",
        intent_type="artifact_lookup",
        tool="list_artifacts",
        confidence=0.86,
    )

    preview = store.preview_match(
        facade="project_context",
        pattern="find recent implemented tasks",
        allowed_intent_types={"artifact_lookup"},
    )

    assert preview is not None
    assert preview["pattern_id"] == pattern_id
    pattern = store.list_patterns(facade="project_context", disabled=False)[0]
    assert pattern["hit_count"] == 0


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


def test_route_pattern_store_blocks_diagnostic_learning_events(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")

    pattern_id = store.record(
        facade="ask_project",
        pattern="why did this route misroute to project_work?",
        intent_type="project_work",
        tool="project_work",
        confidence=0.92,
        source="llm",
        metadata={"diagnostic": True, "source_event_class": "diagnostic"},
    )

    assert pattern_id == ""
    assert store.match(
        facade="ask_project",
        pattern="why did this route misroute to project_work?",
        allowed_intent_types={"project_work"},
    ) is None


def test_route_pattern_store_blocks_synthetic_test_learning_events(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")

    pattern_id = store.record(
        facade="project_context",
        pattern="synthetic weak model scenario should not become production route",
        intent_type="artifact_lookup",
        tool="list_artifacts",
        confidence=0.9,
        source="synthetic_test",
        metadata={"source_event_class": "synthetic_test"},
    )

    assert pattern_id == ""
    assert store.list_patterns(facade="project_context", disabled=False) == []


def test_route_pattern_store_explicit_allow_overrides_blocked_diagnostic_context(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")

    pattern_id = store.record(
        facade="ask_project",
        pattern="operator approved diagnostic phrase as stable alias",
        intent_type="project_context",
        tool="project_context",
        confidence=0.8,
        source="llm",
        metadata={"diagnostic": True, "allow_learning": True},
    )

    assert pattern_id
    match = store.match(
        facade="ask_project",
        pattern="operator approved diagnostic phrase as stable alias",
        allowed_intent_types={"project_context"},
    )
    assert match is not None
    assert match["metadata"]["learning_eligibility"]["decision"] == "explicit_allow"


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


def test_route_pattern_store_hygiene_report_flags_unsafe_active_patterns(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")

    low_confidence_id = store.record(
        facade="project_work",
        pattern="weakly inferred next work phrase",
        intent_type="next_priority",
        tool="list_open_tasks",
        confidence=0.1,
        source="operator_feedback",
    )
    stale_id = store.record(
        facade="project_context",
        pattern="old fuzzy artifact lookup",
        intent_type="artifact_lookup",
        tool="list_artifacts",
        confidence=0.4,
        source="llm",
    )
    contaminated_id = store.record(
        facade="ask_project",
        pattern="operator approved diagnostic misroute phrase",
        intent_type="project_work",
        tool="project_work",
        confidence=0.8,
        source="llm",
        metadata={"allow_learning": True, "diagnostic": True},
    )
    mismatch_id = store.record(
        facade="project_work",
        pattern="create task through old route",
        intent_type="create_task",
        tool="record_work_result",
        confidence=0.8,
        source="operator_feedback",
    )
    weak_provenance_id = store.record(
        facade="project_verify",
        pattern="maybe restart check maybe tests",
        intent_type="verify_or_live_validate",
        tool="get_task_execution_context",
        confidence=0.8,
        source="llm",
    )

    old_timestamp = 1.0
    with store._lock:
        store._conn.execute(
            "UPDATE route_patterns SET updated_at = ?, created_at = ? WHERE id = ?",
            (old_timestamp, old_timestamp, stale_id),
        )
        store._conn.commit()

    report = store.hygiene_report(
        known_tools={"list_open_tasks", "list_artifacts", "project_work", "record_work_result", "get_task_execution_context"},
        limit=50,
    )
    findings_by_type = {item["type"]: item for item in report["findings"]}

    assert findings_by_type["very_low_confidence_pattern"]["pattern_id"] == low_confidence_id
    assert findings_by_type["no_hit_low_evidence_pattern"]["pattern_id"] == stale_id
    assert findings_by_type["diagnostic_or_meta_contamination"]["pattern_id"] == contaminated_id
    assert findings_by_type["route_tool_mismatch"]["pattern_id"] == mismatch_id
    assert findings_by_type["route_tool_mismatch"]["expected_tool"] == "mailbox_submit"
    assert any(
        item["type"] == "weak_learning_provenance" and item["pattern_id"] == weak_provenance_id
        for item in report["findings"]
    )


def test_route_pattern_store_hygiene_report_summarizes_finding_types_and_safe_dispositions(tmp_path):
    store = RoutePatternStore(tmp_path / "route_patterns.db")
    store.record(
        facade="project_work",
        pattern="low confidence route fixture",
        intent_type="next_priority",
        tool="list_open_tasks",
        confidence=0.05,
        source="operator_feedback",
    )
    store.record(
        facade="project_context",
        pattern="diagnostic route_hygiene fixture",
        intent_type="artifact_lookup",
        tool="list_artifacts",
        confidence=0.8,
        metadata={"allow_learning": True},
    )

    report = store.hygiene_report(known_tools={"list_open_tasks", "list_artifacts"}, limit=20)

    assert report["summary"]["finding_types"]["very_low_confidence_pattern"] == 1
    assert report["summary"]["finding_types"]["diagnostic_or_meta_contamination"] == 1
    assert {item["disposition"] for item in report["findings"]} <= {"observe", "request-feedback", "quarantine", "disable"}
    assert all("recommended_action" in item for item in report["findings"])
