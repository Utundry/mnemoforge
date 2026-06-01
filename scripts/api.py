import argparse
import json
import sys
from pathlib import Path

import httpx

# Add the project root to the import path when the script is run directly.
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from app.config import settings


def main():
    parser = argparse.ArgumentParser(description="SloplessCode API CLI")
    parser.add_argument("method", choices=["GET", "POST", "DELETE", "PATCH"], help="HTTP method")
    parser.add_argument("endpoint", help="API endpoint, e.g. /knowledge-tree/slice")
    parser.add_argument("-d", "--data", help="JSON data string", default=None)

    args = parser.parse_args()

    # Build the local API URL from runtime settings.
    port = getattr(settings, "server_port", 8000)
    prefix = getattr(settings, "api_prefix", "/api/v1")
    url = f"http://localhost:{port}{prefix}{args.endpoint}"

    # Read the API key from settings.
    headers = {}
    if settings.api_key:
        headers["X-Api-Key"] = settings.api_key

    # Parse JSON safely before sending the request.
    payload = None
    if args.data:
        try:
            payload = json.loads(args.data)
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            sys.exit(1)

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method=args.method, url=url, headers=headers, json=payload)
            print(f"Status: {response.status_code}")
            try:
                # Keep non-ASCII response content readable for localized data.
                print(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except Exception:
                print(response.text)
    except Exception as e:
        print(f"Connection error: {e}")


if __name__ == "__main__":
    main()
