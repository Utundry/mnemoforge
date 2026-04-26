# Supermemory

Local-first project memory and coordination server for AI coding agents.

Supermemory is not just a long chat log or a bigger prompt window. It gives agents a shared memory layer for:
- semantic retrieval
- project-scoped context
- governed knowledge such as laws, runtime hints, and improvements
- MCP access for external clients
- storage trust, integrity, and hygiene workflows

Stack: FastAPI + Qdrant + SQLite + Ollama.

## Why Supermemory

Use Supermemory when you want agents to work with durable project memory instead of starting every session from scratch.

What it does well:
- project-aware memory, not only user-profile memory
- local-first operation with MCP access
- governed project knowledge and project laws
- external-project bootstrap and readiness assessment
- storage trust tooling: integrity, hygiene, remediation, backup/restore

## Quick Start

- Server setup: `SETUP.md`
- Client setup: `CLIENT_SETUP.md`
- Public alpha status: `STATUS.md`
- External-project roadmap: `docs/EXTERNAL_PROJECT_ROADMAP.md`
- Safe demo dataset: `demo/README.md`

### Docker Compose

```bash
docker compose up -d
```

Default ports:
- Qdrant: `6333` / `6334`
- API, dev/live-mount service: `8000`
- API, baked production image: `8001`

Health probe:

```bash
curl http://localhost:8000/api/v1/health
```

Canonical smoke probe:

```bash
python scripts/mcp_smoke.py --server http://localhost:8000
```

Isolated remote MCP e2e probe:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_docker_remote_mcp_e2e.ps1
```

This uses the Docker Compose `test` profile (`memory-server-test`, `qdrant-test`,
`mcp-e2e-test-runner`) with disposable test storage so replay/e2e fixtures do not
pollute the working `qdrant_data` database.

## MCP

Two transports are available:

- `SSE`
  `.mcp.json` points to `http://localhost:8000/mcp/sse`
- `STDIO`
  `python -m mcp.server`

## Public Alpha Defaults

Recommended defaults for a public alpha:
- use the safe demo dataset in `demo/`
- do not ship live service data
- keep experimental modules disabled by default
- enable `API_KEY` when the server is reachable outside localhost

Current recommended `DISABLED_MODULES` baseline:

```env
DISABLED_MODULES=auto_memory,code_search,layout_fixer,log_filter,openai_compat
```

## Security

Set these in `.env` when not running purely local:

- `API_KEY`: require `X-Api-Key` on non-exempt endpoints
- `INGEST_ALLOWED_ROOTS`: restrict filesystem-reading routes
- `MAX_REQUEST_SIZE_MB`: reject oversized request bodies
- `LLM_RATE_LIMIT_PER_MIN`: per-IP limit for LLM-heavy routes

If you expose the server on a network interface such as `0.0.0.0`, enable `API_KEY` at minimum.
