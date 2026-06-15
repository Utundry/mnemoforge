from app.services.intent_polarity import analyze_intent_polarity


SIGNALS = {
    "available": ("available", "unclaimed", "free"),
    "claimed": ("claimed", "occupied", "busy"),
}


def test_intent_polarity_separates_positive_and_negative_signals() -> None:
    result = analyze_intent_polarity(
        "Show available work without claimed tasks.",
        signals=SIGNALS,
    )

    assert result.positive == frozenset({"available"})
    assert result.negative == frozenset({"claimed"})
    assert result.contradictory == frozenset()
    assert result.evidence()["matches"][1]["negator"] == "without"


def test_intent_polarity_reports_contradictory_signal() -> None:
    result = analyze_intent_polarity(
        "Show claimed tasks, but do not include claimed tasks from the old queue.",
        signals=SIGNALS,
    )

    assert result.positive == frozenset({"claimed"})
    assert result.negative == frozenset({"claimed"})
    assert result.contradictory == frozenset({"claimed"})


def test_intent_polarity_does_not_confuse_action_verb_with_state_signal() -> None:
    result = analyze_intent_polarity(
        "List all open tasks. Do not claim or modify anything.",
        signals=SIGNALS,
    )

    assert result.positive == frozenset()
    assert result.negative == frozenset()


def test_intent_polarity_does_not_cross_punctuation_scope_boundary() -> None:
    result = analyze_intent_polarity(
        "Do not modify. Show claimed tasks.",
        signals=SIGNALS,
    )

    assert result.positive == frozenset({"claimed"})
    assert result.negative == frozenset()
