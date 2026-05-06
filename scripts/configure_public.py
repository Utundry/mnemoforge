from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.public_release_config import render_public_env, validate_public_env


PROVIDER_DEFAULTS = {
    "none": {
        "CLOUD_LLM_PROVIDER": "",
        "CLOUD_LLM_API_KEY": "",
        "CLOUD_LLM_MODEL": "",
        "CLOUD_LLM_BASE_URL": "",
    },
    "deepseek": {
        "CLOUD_LLM_PROVIDER": "deepseek",
        "CLOUD_LLM_MODEL": "deepseek-chat",
        "CLOUD_LLM_BASE_URL": "https://api.deepseek.com",
    },
    "openai-compatible": {
        "CLOUD_LLM_PROVIDER": "openai-compatible",
        "CLOUD_LLM_MODEL": "",
        "CLOUD_LLM_BASE_URL": "",
    },
}


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value if value else default


def _ask_yes_no(prompt: str, *, default: bool) -> bool:
    marker = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{marker}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "true", "1"}


def _build_overrides(args: argparse.Namespace, *, interactive: bool) -> dict[str, str]:
    project_id = args.project_id or "mnemoforge"
    http_port = str(args.http_port or "8000")
    api_key = args.api_key or ""
    provider = args.cloud_provider or "none"
    cloud_api_key = args.cloud_api_key or ""
    cloud_model = args.cloud_model or ""
    cloud_base_url = args.cloud_base_url or ""
    local_fallback = not args.no_local_llm

    if interactive:
        project_id = _ask("Project id", project_id)
        http_port = _ask("HTTP port on this machine", http_port)
        if _ask_yes_no("Generate an API key for protected endpoints", default=bool(api_key)):
            api_key = api_key or secrets.token_urlsafe(24)
        else:
            api_key = _ask("API key, blank for localhost-only experiments", api_key)
        local_fallback = _ask_yes_no("Enable local LLM fallback through host Ollama/LM Studio", default=local_fallback)
        provider = _ask("Cloud provider: none, deepseek, or openai-compatible", provider)
        if provider != "none":
            cloud_api_key = _ask("Cloud API key", cloud_api_key)
            cloud_model = _ask("Cloud model", cloud_model or PROVIDER_DEFAULTS.get(provider, {}).get("CLOUD_LLM_MODEL", ""))
            cloud_base_url = _ask(
                "Cloud base URL",
                cloud_base_url or PROVIDER_DEFAULTS.get(provider, {}).get("CLOUD_LLM_BASE_URL", ""),
            )

    if provider not in PROVIDER_DEFAULTS:
        raise ValueError(f"Unsupported cloud provider: {provider}")

    overrides = {
        "SELF_PROJECT_ID": project_id,
        "SERVER_PORT": "8000",
        "API_KEY": api_key,
        "LLM_GATEWAY_ENABLE_LOCAL_FALLBACK": "1" if local_fallback else "0",
    }
    overrides.update(PROVIDER_DEFAULTS[provider])
    if provider != "none":
        overrides["CLOUD_LLM_API_KEY"] = cloud_api_key
        if cloud_model:
            overrides["CLOUD_LLM_MODEL"] = cloud_model
        if cloud_base_url:
            overrides["CLOUD_LLM_BASE_URL"] = cloud_base_url

    # docker-compose.user.yml consumes this value; it is intentionally outside
    # the app's public-safe env set, so append it after render_public_env.
    overrides["MNEMOFORGE_HTTP_PORT"] = http_port
    return overrides


def render_user_env(args: argparse.Namespace, *, interactive: bool, output_name: str = ".env.user") -> str:
    overrides = _build_overrides(args, interactive=interactive)
    http_port = overrides.pop("MNEMOFORGE_HTTP_PORT")
    text = render_public_env(overrides)
    report = validate_public_env(text)
    if report["missing_required"] or report["forbidden_present"]:
        raise ValueError(f"Generated env failed validation: {report}")
    return text.rstrip() + f"\nMNEMOFORGE_HTTP_PORT={http_port}\nMNEMOFORGE_USER_ENV_FILE={output_name}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a first-user MnemoForge .env file.")
    parser.add_argument("--output", default=".env.user", help="Output env file. Defaults to .env.user.")
    parser.add_argument("--template", default=".env.public.example", help="Public env template path.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output file.")
    parser.add_argument("--non-interactive", action="store_true", help="Use defaults and explicit flags only.")
    parser.add_argument("--project-id", help="SELF_PROJECT_ID value.")
    parser.add_argument("--http-port", type=int, help="Host HTTP port for docker-compose.user.yml.")
    parser.add_argument("--api-key", help="API key for protected endpoints.")
    parser.add_argument("--generate-api-key", action="store_true", help="Generate an API key without prompting.")
    parser.add_argument("--no-local-llm", action="store_true", help="Disable local LLM fallback.")
    parser.add_argument("--cloud-provider", choices=sorted(PROVIDER_DEFAULTS), help="Optional cloud provider preset.")
    parser.add_argument("--cloud-api-key", help="Cloud provider API key.")
    parser.add_argument("--cloud-model", help="Cloud provider model.")
    parser.add_argument("--cloud-base-url", help="Cloud provider base URL.")
    args = parser.parse_args()

    project_root = _PROJECT_ROOT
    output_env_ref = str(Path(args.output))
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path
    template_path = Path(args.template)
    if not template_path.is_absolute():
        template_path = project_root / template_path

    if output_path.exists() and not args.force:
        print(f"Refusing to overwrite existing file: {output_path}", file=sys.stderr)
        return 2
    if not template_path.exists():
        print(f"Missing template: {template_path}", file=sys.stderr)
        return 2

    if args.generate_api_key and not args.api_key:
        args.api_key = secrets.token_urlsafe(24)

    try:
        text = render_user_env(args, interactive=not args.non_interactive, output_name=output_env_ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_path.write_text(text, encoding="utf-8")
    port = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("MNEMOFORGE_HTTP_PORT=")), "8000")
    env_file = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("MNEMOFORGE_USER_ENV_FILE=")), ".env.user")
    print(f"Wrote {output_path}")
    print("Start MnemoForge with:")
    print(f"  docker compose --env-file {env_file} -f docker-compose.user.yml up -d")
    print("Check health with:")
    print(f"  curl http://localhost:{port}/api/v1/health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
