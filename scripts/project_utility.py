from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).with_name("project_utilities.json")
REQUIRED_UTILITY_FIELDS = (
    "title",
    "category",
    "purpose",
    "parameters",
    "constraints",
    "risk",
    "confirmation",
    "verification",
)


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    utilities = data.get("utilities")
    if not isinstance(utilities, list):
        raise ValueError("project utility catalog must contain a utilities list")
    seen: set[str] = set()
    for utility in utilities:
        utility_id = str(utility.get("id") or "").strip()
        command = utility.get("command")
        if not utility_id or utility_id in seen:
            raise ValueError(f"invalid or duplicate utility id: {utility_id!r}")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError(f"utility {utility_id} must define a non-empty string command")
        missing = [field for field in REQUIRED_UTILITY_FIELDS if field not in utility]
        if missing:
            raise ValueError(f"utility {utility_id} is missing contract fields: {', '.join(missing)}")
        if not isinstance(utility["parameters"], list) or not isinstance(utility["constraints"], list):
            raise ValueError(f"utility {utility_id} parameters and constraints must be lists")
        seen.add(utility_id)
    return data


def find_utility(catalog: dict[str, Any], utility_id: str) -> dict[str, Any]:
    for utility in catalog["utilities"]:
        if utility["id"] == utility_id:
            return utility
    raise KeyError(utility_id)


def format_command(parts: list[str]) -> str:
    return subprocess_list2cmdline(parts) if sys.platform == "win32" else shlex.join(parts)


def subprocess_list2cmdline(parts: list[str]) -> str:
    # Keep Windows rendering identical to subprocess without importing execution behavior.
    import subprocess

    return subprocess.list2cmdline(parts)


def _print_list(catalog: dict[str, Any], *, as_json: bool) -> None:
    utilities = catalog["utilities"]
    if as_json:
        print(json.dumps(utilities, ensure_ascii=False, indent=2))
        return
    for utility in utilities:
        print(f"{utility['id']}: {utility['title']}")


def _print_utility(utility: dict[str, Any], *, command_only: bool, as_json: bool) -> None:
    if command_only:
        print(format_command(utility["command"]))
        for command in utility.get("follow_up_commands") or []:
            print(format_command(command))
        return
    if as_json:
        print(json.dumps(utility, ensure_ascii=False, indent=2))
        return
    print(f"{utility['id']}: {utility['title']}")
    print(f"purpose: {utility['purpose']}")
    print(f"command: {format_command(utility['command'])}")
    for command in utility.get("follow_up_commands") or []:
        print(f"follow-up: {format_command(command)}")
    print(f"confirmation: {utility.get('confirmation', 'unspecified')}")
    print(f"risk: {utility.get('risk', '')}")
    print(f"verification: {utility.get('verification', '')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover governed repository development utilities.")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list")
    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("utility_id")
    command_parser = subparsers.add_parser("command")
    command_parser.add_argument("utility_id")
    args = parser.parse_args()

    try:
        catalog = load_catalog(args.catalog)
        if args.action == "list":
            _print_list(catalog, as_json=args.json)
        else:
            utility = find_utility(catalog, args.utility_id)
            _print_utility(utility, command_only=args.action == "command", as_json=args.json)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"project utility error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
