# SloplessCode Setup

This guide describes the public alpha setup path for running SloplessCode from a fresh clone.

## Requirements

- Docker Desktop with Docker Compose
- Git
- Optional: Python 3.13 for local scripts and tests
- Optional: local LLM services such as Ollama or LM Studio
- Optional: cloud LLM credentials for OpenAI-compatible providers

## Quick Start

Clone the repository, generate a local `.env`, and start the user-facing Compose stack:

```powershell
git clone https://github.com/Utundry/sloplesscode.git
cd sloplesscode
python scripts/configure_public.py --non-interactive
docker compose --env-file .env.user -f docker-compose.user.yml up -d
```

For an interactive setup wizard, run `python scripts/configure_public.py` without `--non-interactive`.

Default local endpoints:

- API and MCP service: `http://localhost:8000`
- Qdrant HTTP API: `http://localhost:6333`
- Qdrant gRPC API: `http://localhost:6334`

After restarting Docker Desktop or recreating the stack, allow up to 120 seconds before judging health or logs. Qdrant and background services can need a short warmup window.

## Docker Hub Image

The published image is used by `docker-compose.user.yml` and is also available directly as:

```powershell
docker pull caveboy/sloplesscode:latest
```

During the rename transition, the same image is also tagged as
`caveboy/mnemoforge:latest` so older deployments can keep working while new
clean-machine installs validate the SloplessCode image.

For contributor development, use the source-building stack:

```powershell
Copy-Item .env.public.example .env
docker compose up -d
```

The contributor stack includes `memory-server-dev` with source mounts. Ordinary users should prefer `docker-compose.user.yml`.

## Configuration

Start with `.env.public.example` for public/local use. It keeps optional and experimental modules disabled by default and does not include private credentials.

The easiest path is the configurator:

```powershell
python scripts/configure_public.py
```

Important settings:

- `API_KEY`: set this before exposing the service outside localhost.
- `QDRANT_HOST` and `QDRANT_PORT`: point to your Qdrant instance.
- `CLOUD_LLM_PROVIDER`, `CLOUD_LLM_API_KEY`, `CLOUD_LLM_MODEL`, `CLOUD_LLM_BASE_URL`: configure a generic cloud provider.
- `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`: provider-specific keys used by optional profiles.
- `LLM_GATEWAY_ENABLE_LOCAL_FALLBACK`: keep local LLM fallback enabled when local services are available.

Do not commit a populated `.env` file.

## MCP Clients

SSE endpoint:

```text
http://localhost:8000/mcp/sse
```

The checked-in `.mcp.json` is a localhost example without secrets:

```json
{
  "mcpServers": {
    "sloplesscode": {
      "type": "sse",
      "url": "http://localhost:8000/mcp/sse"
    }
  }
}
```

If you set `API_KEY`, configure your MCP client to send:

```text
X-Api-Key: <your key>
```

## Health Checks

Check the API:

```powershell
curl http://localhost:8000/api/v1/health
```

Run an MCP smoke check:

```powershell
python scripts/mcp_smoke.py --server http://localhost:8000
```

Inspect service logs:

```powershell
docker compose --env-file .env.user -f docker-compose.user.yml logs sloplesscode --tail 120
docker compose --env-file .env.user -f docker-compose.user.yml logs qdrant --tail 120
```

Provider connection failures are expected as warnings when optional LLM services are disabled. The server should remain healthy when at least one configured LLM path is available, and health output should show the available providers.

## Tests

Run focused Docker-based tests:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_pytest_docker.ps1 tests/test_learning_store_writebehind.py -q
```

Run release artifact checks before publishing:

```powershell
python scripts/audit_release_artifacts.py
python -m scripts.publish_docker_image --repository caveboy/sloplesscode --tag latest --check
```

## Troubleshooting

- If the service looks unhealthy immediately after a Docker restart, wait up to 120 seconds and retry.
- If you are using the contributor stack, replace `docker-compose.user.yml` commands with plain `docker compose`.
- If MCP calls return unauthorized responses, check `API_KEY` and the `X-Api-Key` client header.
- If Qdrant fails to start, inspect the `qdrant_data` volume or recreate it for a clean local environment.
- If cloud LLM calls fail, verify the provider base URL, model name, and API key environment variable.
- If local LLM calls fail, confirm Ollama or LM Studio is running and that the configured model is available.

## Security Notes

- Do not expose the service publicly without an `API_KEY`.
- Do not commit `.env`, local databases, logs, or `qdrant_data`.
- Treat memory contents as potentially sensitive user data.

## Author And Contact

SloplessCode is created and maintained by Nikolai Laptev.

- Email: `caveboy@yandex.ru`
- Docker Hub: `caveboy/sloplesscode`
- GitHub repository: `Utundry/sloplesscode`
