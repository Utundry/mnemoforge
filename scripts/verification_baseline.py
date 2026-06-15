from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from scripts.verification_plan import BASELINE_PATH, _baseline_summary


def classify_failures(
    failure_ids: list[str],
    baseline: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    summary = _baseline_summary(baseline, today=today)
    active = set(summary["active_node_ids"])
    expired = set(summary["expired_node_ids"])
    failures = sorted({failure.strip() for failure in failure_ids if failure.strip()})
    return {
        "new_failures": [failure for failure in failures if failure not in active and failure not in expired],
        "registered_failures": [failure for failure in failures if failure in active],
        "expired_baseline_failures": [failure for failure in failures if failure in expired],
        "invalid_baseline_entries": summary["invalid_entries"],
        "baseline": summary,
        "ok": not any(failure not in active for failure in failures) and not summary["invalid_entries"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify exact pytest failures against the baseline registry.")
    parser.add_argument("--failure", action="append", default=[])
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report = classify_failures(args.failure, baseline)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"verification baseline error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"new: {len(report['new_failures'])}")
        print(f"registered: {len(report['registered_failures'])}")
        print(f"expired: {len(report['expired_baseline_failures'])}")
        print(f"invalid baseline entries: {len(report['invalid_baseline_entries'])}")
        for failure in report["new_failures"]:
            print(f"NEW {failure}")
        for failure in report["expired_baseline_failures"]:
            print(f"EXPIRED {failure}")
        for failure in report["registered_failures"]:
            print(f"BASELINE {failure}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
