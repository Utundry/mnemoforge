from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from scripts.project_utility import format_command


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = Path(__file__).with_name("verification_policy.json")
BASELINE_PATH = Path(__file__).with_name("verification_baseline.json")
LEVEL_ORDER = {"none": 0, "focused": 1, "affected": 2, "release": 3, "full": 4}
NO_CHANGES_REASON = "No changed files were detected; verification is a no-op."


@dataclass(slots=True)
class VerificationPlan:
    changed_files: list[str]
    tests: list[str]
    level: str
    matched_rules: list[str]
    reasons: list[str]
    baseline: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        command = []
        if self.tests:
            command = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "scripts\\run_pytest_docker.ps1",
                "-NoBuild",
                *self.tests,
                "-q",
            ]
        return {
            "changed_files": self.changed_files,
            "selection": {
                "level": self.level,
                "tests": self.tests,
                "matched_rules": self.matched_rules,
                "reasons": self.reasons,
            },
            "command": command,
            "command_text": format_command(command) if command else "",
            "baseline": self.baseline,
        }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def _matches(path: str, pattern: str) -> bool:
    normalized_pattern = _normalize_path(pattern)
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, normalized_pattern)


def _git_name_list(args: list[str]) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or f"git {' '.join(args)} failed").strip())
    return [_normalize_path(line) for line in completed.stdout.splitlines() if line.strip()]


def _git_changed_files(base: str) -> list[str]:
    paths: set[str] = set()
    for args in (
        ["diff", "--name-only", f"{base}...HEAD"],
        ["diff", "--name-only"],
        ["diff", "--cached", "--name-only"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        paths.update(_git_name_list(args))
    return sorted(paths)


def _baseline_summary(data: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    current = today or date.today()
    active: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for entry in data.get("failures") or []:
        node_id = str(entry.get("node_id") or "").strip()
        required = ("owner", "reason", "first_seen", "last_seen", "review_due", "disposition")
        if not node_id or node_id in seen or any(not str(entry.get(field) or "").strip() for field in required):
            invalid.append(node_id or "<missing-node-id>")
            continue
        seen.add(node_id)
        try:
            review_due = date.fromisoformat(str(entry["review_due"]))
        except ValueError:
            invalid.append(node_id)
            continue
        (expired if review_due < current else active).append(entry)
    return {
        "active_count": len(active),
        "expired_count": len(expired),
        "invalid_entries": invalid,
        "active_node_ids": [entry["node_id"] for entry in active],
        "expired_node_ids": [entry["node_id"] for entry in expired],
    }


def build_plan(
    changed_files: list[str],
    *,
    policy: dict[str, Any],
    baseline: dict[str, Any],
) -> VerificationPlan:
    normalized = sorted({_normalize_path(path) for path in changed_files if _normalize_path(path)})
    selected_tests: list[str] = []
    matched_rules: list[str] = []
    reasons: list[str] = []
    level = "focused"

    if not normalized:
        return VerificationPlan(
            changed_files=[],
            tests=[],
            level="none",
            matched_rules=[],
            reasons=[NO_CHANGES_REASON],
            baseline=_baseline_summary(baseline),
        )

    for rule in policy.get("rules") or []:
        matched_paths = [
            path for path in normalized if any(_matches(path, pattern) for pattern in rule.get("paths") or [])
        ]
        if not matched_paths:
            continue
        matched_rules.append(str(rule["id"]))
        for test in rule.get("tests") or []:
            if test not in selected_tests:
                selected_tests.append(test)
        rule_level = str(rule.get("level") or "focused")
        if LEVEL_ORDER.get(rule_level, 0) > LEVEL_ORDER.get(level, 0):
            level = rule_level
        reasons.append(
            str(rule.get("reason") or f"{rule['id']} matched: {', '.join(matched_paths)}")
        )

    if not matched_rules:
        default = policy["default"]
        selected_tests.extend(default.get("tests") or [])
        level = str(default.get("level") or "focused")
        reasons.append(str(default.get("reason") or "No explicit dependency rule matched."))

    return VerificationPlan(
        changed_files=normalized,
        tests=selected_tests,
        level=level,
        matched_rules=matched_rules,
        reasons=reasons,
        baseline=_baseline_summary(baseline),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Select the smallest sufficient Docker pytest contour.")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument(
        "--base",
        default="HEAD",
        help="Committed comparison base. Defaults to HEAD so normal planning covers current index/worktree/untracked changes.",
    )
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        changed_files = args.changed_file or _git_changed_files(args.base)
        plan = build_plan(
            changed_files,
            policy=_load_json(args.policy),
            baseline=_load_json(args.baseline),
        ).as_dict()
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"verification planner error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"level: {plan['selection']['level']}")
        print(f"rules: {', '.join(plan['selection']['matched_rules']) or 'default'}")
        for reason in plan["selection"]["reasons"]:
            print(f"reason: {reason}")
        print(f"command: {plan['command_text']}")
        baseline = plan["baseline"]
        print(
            "baseline: "
            f"active={baseline['active_count']} expired={baseline['expired_count']} "
            f"invalid={len(baseline['invalid_entries'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
