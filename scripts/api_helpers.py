import json
import os
from urllib.parse import urlencode
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


def request_json(
    endpoint: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    json_payload: dict | None = None,
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
    except Exception as exc:
        return {"error": str(exc)}


def get_json(endpoint: str, *, params: dict | None = None, auth: bool = False) -> dict:
    return request_json(endpoint, method="GET", params=params, auth=auth)


def post_json(endpoint: str, *, json_payload: dict | None = None, auth: bool = False) -> dict:
    return request_json(endpoint, method="POST", json_payload=json_payload, auth=auth)
