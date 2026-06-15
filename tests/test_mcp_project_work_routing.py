from app.services.mcp_project_work_routing import project_work_route


def _route(intent: str, intent_type: str, **args):
    return project_work_route(
        {"project": "alpha", "intent": intent, **args},
        llm_decision={
            "intent_type": intent_type,
            "confidence": 0.95,
            "matched_example": "test",
        },
        scorer_meta={
            "backend_requested": "llm",
            "backend_used": "llm",
            "llm_attempted": True,
            "fallback_reason": "",
        },
    )


def test_list_all_ignores_negated_claim_action() -> None:
    route = _route(
        "List all open tasks for review. Do not claim, close, or modify anything.",
        "list_all_tasks",
    )

    assert route["payload"]["claim_filter"] == "all"
    assert route["claim_filter_resolution"]["source"] == "natural_language"
    assert route["claim_filter_resolution"]["polarity"]["positive"] == ["all"]
    assert "Final claim_filter=all." in route["reason"]


def test_negative_claimed_state_selects_available() -> None:
    route = _route(
        "List open tasks without claimed or occupied work.",
        "list_all_tasks",
    )

    assert route["payload"]["claim_filter"] == "available"
    resolution = route["claim_filter_resolution"]
    assert resolution["available_signal"] is True
    assert resolution["polarity"]["negative"] == ["claimed"]
    assert "Final claim_filter=available." in route["reason"]
    assert "claim_filter=all" not in route["reason"]


def test_explicit_claimed_only_still_selects_claimed() -> None:
    route = _route("Show occupied open tasks.", "list_all_tasks")

    assert route["payload"]["claim_filter"] == "claimed"
    assert route["claim_filter_resolution"]["claimed_signal"] is True
    assert "Final claim_filter=claimed." in route["reason"]


def test_available_and_claimed_together_select_all() -> None:
    route = _route("Show available and claimed open tasks.", "list_all_tasks")

    assert route["payload"]["claim_filter"] == "all"
    assert route["claim_filter_resolution"]["available_signal"] is True
    assert route["claim_filter_resolution"]["claimed_signal"] is True


def test_structured_claim_filter_has_precedence() -> None:
    route = _route(
        "Show available tasks.",
        "next_priority",
        claim_filter="claimed",
    )

    assert route["payload"]["claim_filter"] == "claimed"
    assert route["claim_filter_resolution"]["source"] == "explicit_argument"
    assert "Final claim_filter=claimed." in route["reason"]


def test_learned_claim_filter_resolves_non_english_negation_without_phrase_hardcode() -> None:
    route = _route(
        "Не предлагай claimed/occupied работу.",
        "next_priority",
        _learned_claim_filter="available",
        _claim_filter_learning={
            "pattern_id": "learned-1",
            "backend_used": "learned_exact",
            "matched_by": "exact",
            "score": 1.0,
        },
    )

    assert route["payload"]["claim_filter"] == "available"
    resolution = route["claim_filter_resolution"]
    assert resolution["source"] == "learned_route_parameter"
    assert resolution["learning"]["pattern_id"] == "learned-1"
    assert "Final claim_filter=available." in route["reason"]


def test_structured_claim_filter_overrides_learned_claim_filter() -> None:
    route = _route(
        "Не предлагай claimed работу.",
        "next_priority",
        claim_filter="claimed",
        _learned_claim_filter="available",
    )

    assert route["payload"]["claim_filter"] == "claimed"
    assert route["claim_filter_resolution"]["source"] == "explicit_argument"


def test_start_task_session_forwards_public_recovery_identity() -> None:
    route = _route(
        "Claim and start this task.",
        "start_task_session",
        task_id="task-1",
        agent_id="codex",
        owner_agent="owner-1",
        session_id="session-1",
        work_id="work-1",
        work_token="secret-token",
        agent_fingerprint="fingerprint-1",
        runtime_profile_id="stateless_mcp",
        lease_ttl_seconds=1200,
    )

    assert route["payload"]["owner_agent"] == "owner-1"
    assert route["payload"]["session_id"] == "session-1"
    assert route["payload"]["work_id"] == "work-1"
    assert route["payload"]["work_token"] == "secret-token"
    assert route["payload"]["agent_fingerprint"] == "fingerprint-1"
    assert route["payload"]["runtime_profile_id"] == "stateless_mcp"
    assert route["payload"]["lease_ttl_seconds"] == 1200


def test_finish_task_session_forwards_public_recovery_identity() -> None:
    route = _route(
        "Finish this task session.",
        "finish_task_session",
        task_id="task-1",
        agent_id="codex",
        owner_agent="owner-1",
        session_id="session-1",
        work_id="work-1",
        work_token="secret-token",
    )

    assert route["payload"]["owner_agent"] == "owner-1"
    assert route["payload"]["session_id"] == "session-1"
    assert route["payload"]["work_id"] == "work-1"
    assert route["payload"]["work_token"] == "secret-token"
