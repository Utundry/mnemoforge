from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from api_helpers import BASE_URL, MissingApiKeyError, live_request_json


def _load_payload(value: str | None, payload_file: str | None) -> dict | None:
    if payload_file:
        return json.loads(Path(payload_file).read_text(encoding="utf-8"))
    if value:
        return json.loads(value)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call the live Supermemory API with X-API-Key enabled by default."
    )
    parser.add_argument("endpoint", nargs="?", default="health")
    parser.add_argument("--method", default="GET", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    parser.add_argument("--payload", help="JSON request body.")
    parser.add_argument("--payload-file", help="Path to a JSON request body.")
    parser.add_argument("--no-auth", action="store_true", help="Disable X-API-Key injection.")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    try:
        result = live_request_json(
            args.endpoint,
            method=args.method,
            json_payload=_load_payload(args.payload, args.payload_file),
            auth=not args.no_auth,
            timeout=args.timeout,
        )
    except MissingApiKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps({"server": BASE_URL, "result": result}, ensure_ascii=False, indent=2))
    return 1 if isinstance(result, dict) and result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
