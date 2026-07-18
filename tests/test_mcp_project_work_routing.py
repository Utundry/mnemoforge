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
        work_handle="wh1.public-handle",
        agent_fingerprint="fingerprint-1",
        runtime_profile_id="stateless_mcp",
        lease_ttl_seconds=1200,
    )

    assert route["payload"]["owner_agent"] == "owner-1"
    assert route["payload"]["session_id"] == "session-1"
    assert route["payload"]["work_id"] == "work-1"
    assert route["payload"]["work_token"] == "secret-token"
    assert route["payload"]["work_handle"] == "wh1.public-handle"
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
        work_handle="wh1.public-handle",
    )

    assert route["payload"]["owner_agent"] == "owner-1"
    assert route["payload"]["session_id"] == "session-1"
    assert route["payload"]["work_id"] == "work-1"
    assert route["payload"]["work_token"] == "secret-token"
    assert route["payload"]["work_handle"] == "wh1.public-handle"


def test_primary_start_action_beats_embedded_create_task_phrase() -> None:
    route = _route(
        "start implementation for task 4decf1b6-b638-43e5-946e-1bbc2af97c71: Prefer lifecycle finish intent over embedded create-task phrase matches",
        "create_task",
        task_id="4decf1b6-b638-43e5-946e-1bbc2af97c71",
        agent_id="codex",
    )

    assert route["intent_type"] == "start_task_session"
    assert route["tool"] == "start_task_session"
    assert route["payload"]["task_id"] == "4decf1b6-b638-43e5-946e-1bbc2af97c71"
    assert route["intent_arbitration"]["demoted_candidate"] == "create_task"
    assert "intent_arbitration:start_task_session" in route["evidence"]


def test_primary_create_task_about_finish_bug_stays_create_task() -> None:
    route = _route(
        "create task to fix finish task route misclassification",
        "create_task",
        task_id="existing-task-id",
    )

    assert route["intent_type"] == "create_task"
    assert route["tool"] == "mailbox_submit"
    assert "intent_arbitration" not in route


def test_cold_start_routes_to_project_readiness() -> None:
    route = project_work_route(
        {"project": "sloplesscode", "intent": "сделай холодный старт sloplesscode"},
        scorer_meta={
            "backend_requested": "lexical",
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
    )

    assert route["intent_type"] == "project_memory_bootstrap"
    assert route["tool"] == "get_project_readiness"
    assert route["mutating"] is False
    assert route["payload"]["project_id"] == "sloplesscode"


def test_initialize_project_memory_routes_to_project_readiness() -> None:
    route = project_work_route(
        {"project": "alpha", "intent": "initialize project memory"},
        scorer_meta={
            "backend_requested": "lexical",
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
    )

    assert route["intent_type"] == "project_memory_bootstrap"
    assert route["tool"] == "get_project_readiness"
    assert route["payload"]["project"] == "alpha"
    assert route["payload"]["project_id"] == "alpha"

def test_completed_but_open_anomaly_routes_to_read_only_repair_finder() -> None:
    route = project_work_route(
        {"project": "alpha", "intent": "find completed but open lifecycle anomalies", "limit": 5},
        scorer_meta={
            "backend_requested": "lexical",
            "backend_used": "lexical",
            "llm_attempted": False,
            "fallback_reason": "",
        },
    )

    assert route["intent_type"] == "lifecycle_anomaly_repair"
    assert route["tool"] == "list_closeable_completed_tail"
    assert route["mutating"] is False
    assert route["payload"] == {"project": "alpha", "close_policy": "strict", "limit": 5}
