from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_LOGIN_URL = "https://hub.docker.com/v2/users/login/"
DEFAULT_REPOSITORY_URL = "https://hub.docker.com/v2/repositories/{repository}/"
DEFAULT_CREDENTIAL_SERVER = "https://index.docker.io/v1/"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _resolve_repository(value: str | None) -> str:
    repository = str(value or os.environ.get("DOCKERHUB_REPOSITORY") or os.environ.get("DOCKER_IMAGE_REPOSITORY") or "").strip()
    if not repository:
        raise ValueError("Docker Hub repository is required. Pass --repository or set DOCKERHUB_REPOSITORY.")
    if repository.startswith("http://") or repository.startswith("https://"):
        raise ValueError("Repository must be a Docker image name, not a URL.")
    if ":" in repository:
        raise ValueError("Repository must not include a tag.")
    if "/" not in repository:
        raise ValueError("Repository should look like namespace/name.")
    return repository


def _resolve_description(value: str | None) -> str:
    description = str(value or os.environ.get("DOCKERHUB_DESCRIPTION") or "").strip()
    return description or "Operational continuity infrastructure for AI coding agents."


def _docker_config_path() -> Path:
    return Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"


def _docker_credential_helper_name(config: dict) -> str:
    helper = str(config.get("credsStore") or "").strip()
    if helper:
        return f"docker-credential-{helper}"
    return ""


def _load_docker_credential(server_url: str = DEFAULT_CREDENTIAL_SERVER) -> tuple[str, str] | None:
    config_path = _docker_config_path()
    if not config_path.exists():
        return None
    try:
        config = json.loads(_read_text(config_path))
    except Exception:
        return None
    helper = _docker_credential_helper_name(config)
    if not helper:
        return None
    completed = subprocess.run(
        [helper, "get"],
        input=server_url,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except Exception:
        return None
    username = str(payload.get("Username") or "").strip()
    secret = str(payload.get("Secret") or "").strip()
    return (username, secret) if username and secret else None


def _resolve_auth() -> tuple[str, str] | str:
    jwt = str(os.environ.get("DOCKERHUB_JWT") or "").strip()
    if jwt:
        return jwt
    username = str(os.environ.get("DOCKERHUB_USERNAME") or "").strip()
    token = str(
        os.environ.get("DOCKERHUB_TOKEN")
        or os.environ.get("DOCKERHUB_PAT")
        or os.environ.get("DOCKERHUB_PASSWORD")
        or ""
    ).strip()
    if username and token:
        return username, token
    credential = _load_docker_credential()
    if credential:
        return credential
    raise ValueError(
        "Docker Hub credentials are required. Set DOCKERHUB_JWT, or DOCKERHUB_USERNAME plus DOCKERHUB_TOKEN/PAT, "
        "or log in with Docker Desktop/CLI."
    )


def _request_json(url: str, *, method: str = "GET", token: str = "", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Docker Hub API {method} {url} failed: HTTP {exc.code}: {detail}") from exc


def _login(auth: tuple[str, str] | str) -> str:
    if isinstance(auth, str):
        return auth
    username, password = auth
    data = _request_json(DEFAULT_LOGIN_URL, method="POST", payload={"username": username, "password": password})
    token = str(data.get("token") or "").strip()
    if not token:
        raise RuntimeError("Docker Hub login did not return a token.")
    return token


def publish_overview(
    *,
    repository: str,
    overview: str,
    description: str,
    dry_run: bool = False,
) -> dict:
    if len(overview) > 25000:
        raise ValueError("Docker Hub full_description must be <= 25000 characters.")
    if dry_run:
        return {
            "repository": repository,
            "description": description,
            "full_description_length": len(overview),
            "dry_run": True,
        }
    token = _login(_resolve_auth())
    url = DEFAULT_REPOSITORY_URL.format(repository=repository)
    return _request_json(
        url,
        method="PATCH",
        token=token,
        payload={"description": description, "full_description": overview},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish Docker Hub repository Overview metadata.")
    parser.add_argument("--repository", help="Docker Hub repository, for example caveboy/sloplesscode.")
    parser.add_argument("--overview-file", default="docs/DOCKERHUB_OVERVIEW.md", help="Markdown file for full_description.")
    parser.add_argument("--description", help="Short Docker Hub repository description.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the payload summary without writing.")
    args = parser.parse_args()

    try:
        repository = _resolve_repository(args.repository)
        overview = _read_text(Path(args.overview_file))
        description = _resolve_description(args.description)
        result = publish_overview(
            repository=repository,
            overview=overview,
            description=description,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
