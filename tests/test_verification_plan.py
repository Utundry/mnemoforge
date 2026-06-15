from datetime import date

from types import SimpleNamespace

from scripts import verification_plan
from scripts.verification_baseline import classify_failures
from scripts.verification_plan import _baseline_summary, build_plan


POLICY = {
    "default": {
        "tests": ["tests/test_project_utility.py"],
        "level": "focused",
        "reason": "default",
    },
    "rules": [
        {
            "id": "tooling",
            "paths": ["scripts/**"],
            "tests": ["tests/test_project_utility.py", "tests/test_verification_plan.py"],
            "level": "focused",
        },
        {
            "id": "shared",
            "paths": ["app/main.py"],
            "tests": ["tests"],
            "level": "full",
            "reason": "shared runtime",
        },
    ],
}


def test_build_plan_selects_focused_tooling_tests():
    plan = build_plan(
        ["scripts/project_utility.py"],
        policy=POLICY,
        baseline={"failures": []},
    )

    assert plan.level == "focused"
    assert plan.matched_rules == ["tooling"]
    assert plan.tests == ["tests/test_project_utility.py", "tests/test_verification_plan.py"]
    assert "run_pytest_docker.ps1" in plan.as_dict()["command_text"]


def test_build_plan_escalates_to_highest_matching_level():
    plan = build_plan(
        ["scripts/project_utility.py", "app/main.py"],
        policy=POLICY,
        baseline={"failures": []},
    )

    assert plan.level == "full"
    assert plan.tests[-1] == "tests"
    assert "shared runtime" in plan.reasons


def test_baseline_requires_exact_reviewable_non_expired_entries():
    summary = _baseline_summary(
        {
            "failures": [
                {
                    "node_id": "tests/test_old.py::test_known",
                    "owner": "maintainer",
                    "reason": "known upstream defect",
                    "first_seen": "2026-01-01",
                    "last_seen": "2026-06-01",
                    "review_due": "2026-07-01",
                    "disposition": "fix",
                },
                {
                    "node_id": "tests/test_expired.py::test_old",
                    "owner": "maintainer",
                    "reason": "old",
                    "first_seen": "2025-01-01",
                    "last_seen": "2025-02-01",
                    "review_due": "2025-03-01",
                    "disposition": "quarantine",
                },
                {"node_id": "tests/test_invalid.py::test_missing_metadata"},
            ]
        },
        today=date(2026, 6, 15),
    )

    assert summary["active_node_ids"] == ["tests/test_old.py::test_known"]
    assert summary["expired_node_ids"] == ["tests/test_expired.py::test_old"]
    assert summary["invalid_entries"] == ["tests/test_invalid.py::test_missing_metadata"]


def test_git_changed_files_combines_commits_worktree_index_and_untracked(monkeypatch):
    outputs = {
        ("diff", "--name-only", "main...HEAD"): "app/main.py\n",
        ("diff", "--name-only"): "scripts/verification_plan.py\n",
        ("diff", "--cached", "--name-only"): "docs/PROJECT_LAW.md\n",
        ("ls-files", "--others", "--exclude-standard"): "tests/test_verification_plan.py\n",
    }

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=outputs[tuple(cmd[1:])], stderr="")

    monkeypatch.setattr(verification_plan.subprocess, "run", fake_run)

    assert verification_plan._git_changed_files("main") == [
        "app/main.py",
        "docs/PROJECT_LAW.md",
        "scripts/verification_plan.py",
        "tests/test_verification_plan.py",
    ]


def test_classify_failures_never_hides_new_or_expired_failures():
    baseline = {
        "failures": [
            {
                "node_id": "tests/test_known.py::test_known",
                "owner": "maintainer",
                "reason": "known",
                "first_seen": "2026-01-01",
                "last_seen": "2026-06-01",
                "review_due": "2026-07-01",
                "disposition": "fix",
            },
            {
                "node_id": "tests/test_old.py::test_old",
                "owner": "maintainer",
                "reason": "old",
                "first_seen": "2025-01-01",
                "last_seen": "2025-02-01",
                "review_due": "2025-03-01",
                "disposition": "quarantine",
            },
        ]
    }

    report = classify_failures(
        [
            "tests/test_known.py::test_known",
            "tests/test_old.py::test_old",
            "tests/test_new.py::test_new",
        ],
        baseline,
        today=date(2026, 6, 15),
    )

    assert report["registered_failures"] == ["tests/test_known.py::test_known"]
    assert report["expired_baseline_failures"] == ["tests/test_old.py::test_old"]
    assert report["new_failures"] == ["tests/test_new.py::test_new"]
    assert report["ok"] is False
