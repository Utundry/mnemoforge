import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
import urllib.request

from dotenv import load_dotenv

load_dotenv()

API_KEY = (
    os.environ.get("MEMORY_SERVER_API_KEY") or
    os.environ.get("API_KEY") or
    os.environ.get("SUPER_MEMORY_API_KEY") or
    os.environ.get("SUPERMEMORY_API_KEY") or
    ""
).strip()
BASE_URL = os.environ.get("SUPERMEMORY_SERVER_URL", "http://localhost:8000").rstrip("/")
API_PREFIX = os.environ.get("API_PREFIX", "/api/v1").strip()
if not API_PREFIX.startswith("/"):
    API_PREFIX = "/" + API_PREFIX
BASE_API_URL = f"{BASE_URL}{API_PREFIX}"


class MissingApiKeyError(RuntimeError):
    """Raised when a live API request requires authentication but no key exists."""


def _build_url(endpoint: str, params: dict | None = None) -> str:
    endpoint = endpoint.lstrip("/")
    url = f"{BASE_API_URL}/{endpoint}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


def get_headers(auth: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/json",
    }
    if auth and API_KEY:
        headers["X-API-Key"] = API_KEY
    return headers


def require_api_key() -> str:
    if API_KEY:
        return API_KEY
    raise MissingApiKeyError(
        "Live API request requires X-API-Key, but no key was found. "
        "Set MEMORY_SERVER_API_KEY, API_KEY, SUPER_MEMORY_API_KEY, or SUPERMEMORY_API_KEY."
    )


def get_live_headers(auth: bool = True) -> dict[str, str]:
    if auth:
        require_api_key()
    return get_headers(auth=auth)


def request_json(
    endpoint: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    json_payload: dict[str, Any] | None = None,
    auth: bool = False,
    timeout: float = 10.0,
) -> dict:
    url = _build_url(endpoint, params)
    data = None
    headers = get_headers(auth=auth)
    if json_payload is not None:
        data = json.dumps(json_payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")
        if exc.code in {401, 403} and "x-api-key" in body.lower():
            return {
                "error": "Live API authentication failed. Check X-API-Key configuration.",
                "status_code": exc.code,
                "body": body,
            }
        return {"error": str(exc), "status_code": exc.code, "body": body}
    except Exception as exc:
        return {"error": str(exc)}


def get_json(endpoint: str, *, params: dict | None = None, auth: bool = False) -> dict:
    return request_json(endpoint, method="GET", params=params, auth=auth)


def post_json(endpoint: str, *, json_payload: dict | None = None, auth: bool = False) -> dict:
    return request_json(endpoint, method="POST", json_payload=json_payload, auth=auth)


def live_request_json(
    endpoint: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    json_payload: dict[str, Any] | None = None,
    auth: bool = True,
    timeout: float = 10.0,
) -> dict:
    get_live_headers(auth=auth)
    return request_json(
        endpoint,
        method=method,
        params=params,
        json_payload=json_payload,
        auth=auth,
        timeout=timeout,
    )


def live_get_json(endpoint: str, *, params: dict | None = None, auth: bool = True) -> dict:
    return live_request_json(endpoint, method="GET", params=params, auth=auth)


def live_post_json(
    endpoint: str,
    *,
    json_payload: dict[str, Any] | None = None,
    auth: bool = True,
) -> dict:
    return live_request_json(endpoint, method="POST", json_payload=json_payload, auth=auth)
