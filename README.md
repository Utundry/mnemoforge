# MnemoForge

Local-first memory, knowledge, and coordination server for AI coding agents.

MnemoForge helps agents keep durable project context across sessions. It is not
just a larger chat history: it stores project-scoped memory, governed knowledge,
task evidence, reusable rules, and MCP-accessible workflows so an agent can pick
up real work without asking the user to reconstruct the whole project again.

> Public alpha: the core server works, but packaging, documentation, and external
> project bootstrap are still being hardened.

## What It Is For

Use MnemoForge when you want AI coding agents to:

- remember project decisions, tasks, verification results, and follow-up work;
- retrieve project context semantically instead of relying only on prompt text;
- maintain governed knowledge such as project laws, runtime hints, and
  improvement records;
- coordinate through MCP tools rather than ad hoc chat summaries;
- keep storage health, integrity, and data hygiene visible;
- work with local LLM providers when available and cloud fallback when configured.

## Core Capabilities

- **Project memory**: semantic memories, task checkpoints, decisions, and
  evidence records scoped by project.
- **MCP server**: SSE and stdio transports for agent clients.
- **Work-session closeout**: stenographer spans, clerk/scribe draft reports, and
  approve-by-reference checkpoints before governed memory mutation.
- **Project governance**: project laws, improvements, task artifacts, readiness
  checks, and operational guidance.
- **Storage trust**: integrity audits, hygiene reports, runtime ownership guards,
  backup/restore helpers, and remediation surfaces.
- **Provider-flexible LLM path**: local Ollama and LM Studio support, plus cloud
  LLM fallback through configurable provider profiles.

## Architecture

MnemoForge is built around a FastAPI service with Qdrant for vector search and
SQLite stores for durable project metadata. Agents interact with it over HTTP or
MCP.

```text
AI agent / MCP client
        |
        |  MCP SSE or stdio
        v
MnemoForge FastAPI server
        |
        +-- Qdrant vector index
        +-- SQLite governed stores
        +-- local/cloud LLM providers
```

## Quick Start

### Option 1: Docker Hub Image

The current public image is published as:

```bash
docker pull caveboy/mnemoforge:latest
```

Immutable commit tags are published alongside `latest`:

```bash
docker pull caveboy/mnemoforge:<git-sha>
```

Run it with a Qdrant container or use the repository `docker-compose.yml` for a
development stack.

### Option 2: Docker Compose From Source

```bash
git clone https://github.com/Utundry/mnemoforge.git
cd mnemoforge
docker compose up -d
```

Default local ports:

- API dev container: `http://localhost:8000`
- API baked container: `http://localhost:8001`
- Qdrant: `6333` and `6334`

Health check:

```bash
curl http://localhost:8000/api/v1/health
```

MCP smoke check:

```bash
python scripts/mcp_smoke.py --server http://localhost:8000
```

## MCP Usage

MnemoForge exposes two MCP transports:

- **SSE**: `http://localhost:8000/mcp/sse`
- **STDIO**: `python -m mcp.server`

Recommended first tools for agents:

- `get_onboarding` for session-specific operating guidance;
- `operational_tray` for state-aware project workflow actions;
- `get_task_execution_context` before implementation or closeout;
- `clerk_draft_report` for review-only closeout drafts from raw notes or
  stenographer spans;
- `approve_checkpoint_draft` to persist an approved draft canonically.

MnemoForge also supports compact MCP tool discovery for clients that should not
load the full tool catalog immediately. See [SETUP.md](SETUP.md) for client
configuration examples.

## LLM Providers

MnemoForge is local-first but not locked to one local service.

Supported provider paths include:

- Ollama for local embeddings/generation;
- LM Studio as a local fallback;
- configurable cloud LLM providers such as DeepSeek through the cloud gateway.

If a local provider is unavailable, the server should surface degraded provider
state while continuing through configured fallbacks where possible. See
[docs/CLOUD_LLM_PROVIDERS.md](docs/CLOUD_LLM_PROVIDERS.md).

## Public Alpha Defaults

For public or shared deployments:

- set `API_KEY` when the server is reachable outside localhost;
- do not publish live `qdrant_data`;
- start from `.env.public.example`;
- keep experimental modules disabled unless you are actively testing them;
- use the safe demo dataset in [demo/](demo/) for examples.

Recommended public-alpha disable list:

```env
DISABLED_MODULES=auto_memory,code_search,layout_fixer,log_filter,openai_compat
```

## Development And Testing

Use Docker-backed tests for DB and MCP integration checks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_pytest_docker.ps1 tests/test_learning_store_writebehind.py -q
```

Remote MCP e2e contour:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_docker_remote_mcp_e2e.ps1
```

The Docker test profile uses disposable Qdrant and SQLite storage, so test runs
do not pollute live `qdrant_data`.

## Documentation

- [SETUP.md](SETUP.md): server setup and local development notes
- [CLIENT_SETUP.md](CLIENT_SETUP.md): client-only MCP setup
- [STATUS.md](STATUS.md): current alpha status and known rough edges
- [docs/CLOUD_LLM_PROVIDERS.md](docs/CLOUD_LLM_PROVIDERS.md): cloud LLM setup
- [docs/EXTERNAL_PROJECT_ROADMAP.md](docs/EXTERNAL_PROJECT_ROADMAP.md):
  roadmap for non-self projects
- [docs/USAGE_CONDITIONS.md](docs/USAGE_CONDITIONS.md): intended use and limits
- [docs/PUBLIC_RELEASE_CHECKLIST.md](docs/PUBLIC_RELEASE_CHECKLIST.md): release
  checklist
- [demo/README.md](demo/README.md): safe demo dataset

## Security Notes

Set these in `.env` for non-local deployments:

- `API_KEY`: require `X-Api-Key` on protected endpoints;
- `INGEST_ALLOWED_ROOTS`: restrict filesystem-reading routes;
- `MAX_REQUEST_SIZE_MB`: reject oversized request bodies;
- `LLM_RATE_LIMIT_PER_MIN`: rate-limit LLM-heavy routes.

Do not expose MnemoForge publicly without authentication and a deliberate data
boundary.

## Author And Contact

MnemoForge is created and maintained by Nikolai Laptev.

- Email: `caveboy@yandex.ru`
- Docker Hub: `caveboy/mnemoforge`
- GitHub repository: `Utundry/mnemoforge`

## Project Name

`MnemoForge` is the public release name. The internal project id remains
`mnemoforge` in task history, storage metadata, and compatibility paths.
