from __future__ import annotations

from app.services.governed_artifact import (
    apply_buffered_revision,
    apply_candidate_fields,
    build_candidate_revision,
    clear_prefixed_candidate_patch,
    discard_buffered_revision,
    extract_prefixed_candidate,
    prefixed_candidate_patch,
    stage_buffered_revision,
)


def test_build_candidate_revision_overrides_only_updated_fields():
    candidate = build_candidate_revision(
        base={"title": "Active", "scope": "project", "evidence": ["a"]},
        updates={"title": "Candidate", "evidence": ["a", "b"], "scope": None},
        fields=("title", "scope", "evidence"),
        proposed_at="2026-03-21T00:00:00+00:00",
    )

    assert candidate == {
        "title": "Candidate",
        "scope": "project",
        "evidence": ["a", "b"],
        "status": "proposed",
        "proposed_at": "2026-03-21T00:00:00+00:00",
    }


def test_prefixed_candidate_helpers_round_trip():
    candidate = {
        "content": "Candidate content",
        "supports": ["a", "b"],
        "confidence": 0.9,
    }
    patch = prefixed_candidate_patch(candidate, fields=("content", "supports", "confidence"))
    assert patch["candidate_content"] == "Candidate content"

    extracted = extract_prefixed_candidate(
        patch,
        fields=("content", "supports", "confidence"),
        required_field="content",
        defaults={"supports": [], "confidence": 0.0},
    )
    assert extracted == candidate
    assert clear_prefixed_candidate_patch(fields=("content", "supports", "confidence")) == {
        "candidate_content": None,
        "candidate_supports": None,
        "candidate_confidence": None,
    }


def test_apply_candidate_fields_prefers_candidate_values():
    applied = apply_candidate_fields(
        effective={"content": "Active", "confidence": 0.5, "supports": ["a"]},
        candidate={"content": "Candidate", "supports": ["a", "b"]},
        fields=("content", "confidence", "supports"),
    )
    assert applied == {
        "content": "Candidate",
        "confidence": 0.5,
        "supports": ["a", "b"],
    }


def test_buffered_revision_helpers_preserve_effective_and_manage_candidate():
    effective, effective_at, candidate, candidate_at = stage_buffered_revision(
        effective_value={"overview": "v1"},
        effective_updated_at="t1",
        replacement_value={"overview": "v2"},
        replacement_updated_at="t2",
        preserve_effective=True,
        empty_factory=dict,
    )
    assert effective == {"overview": "v1"}
    assert effective_at == "t1"
    assert candidate == {"overview": "v2"}
    assert candidate_at == "t2"

    applied = apply_buffered_revision(
        effective_value=effective,
        effective_updated_at=effective_at,
        candidate_value=candidate,
        candidate_updated_at=candidate_at,
        empty_factory=dict,
    )
    assert applied == ({"overview": "v2"}, "t2", {}, None)

    discarded = discard_buffered_revision(
        effective_value=effective,
        effective_updated_at=effective_at,
        candidate_value=candidate,
        empty_factory=dict,
    )
    assert discarded == ({"overview": "v1"}, "t1", {}, None)
