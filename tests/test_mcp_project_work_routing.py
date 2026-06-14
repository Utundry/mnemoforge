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
