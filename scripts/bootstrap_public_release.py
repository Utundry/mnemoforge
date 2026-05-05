from __future__ import annotations

import argparse
from pathlib import Path
import sys

from scripts.public_release_config import PUBLIC_TEMPLATE_NAME, render_public_env, validate_public_env


def _parse_override(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Override must be KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a safe public-release .env from the public template."
    )
    parser.add_argument("--output", default=".env", help="Path to write the generated env file.")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Override a key as KEY=VALUE. May be supplied multiple times.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the generated output and exit without writing.",
    )
    parser.add_argument(
        "--template",
        default=PUBLIC_TEMPLATE_NAME,
        help="Template file to read from (defaults to .env.public.example).",
    )
    args = parser.parse_args()

    template_path = Path(args.template)
    if not template_path.is_absolute():
        template_path = Path(__file__).resolve().parents[1] / template_path
    if not template_path.exists():
        print(f"Template not found: {template_path}", file=sys.stderr)
        return 2

    overrides = _parse_override(args.set)
    text = render_public_env(overrides=overrides, template_path=template_path)
    report = validate_public_env(text)
    if report["missing_required"] or report["forbidden_present"] or report["internal_defaults_present"]:
        print("Public release env validation failed:", file=sys.stderr)
        if report["missing_required"]:
            print(f"  missing_required: {', '.join(report['missing_required'])}", file=sys.stderr)
        if report["forbidden_present"]:
            print(f"  forbidden_present: {', '.join(report['forbidden_present'])}", file=sys.stderr)
        if report["internal_defaults_present"]:
            print(f"  internal_defaults_present: {', '.join(report['internal_defaults_present'])}", file=sys.stderr)
        return 1

    if args.check:
        print(text, end="")
        return 0

    output_path = Path(args.output)
    output_path.write_text(text, encoding="utf-8")
    print(f"Wrote public release config to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
