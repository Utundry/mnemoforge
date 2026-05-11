# Mnemoforge

**Operational Continuity Infrastructure for AI Coding Agents**

**AI agents stop failing when operational continuity survives interruption.**

---

### The Real Problem

Powerful AI agents like Claude, Cursor, and Codex work great… until something breaks:

- Session ends or crashes
- You hit subscription limits
- You switch between models
- You come back to the project after a few days
- Requirements evolve during development

Result? Lost context, duplicated work, broken assumptions, and hours of re-explanation.

### The Solution

**Mnemoforge** is a **distributed operational cognition layer** that gives your AI agents true continuity across sessions, models, machines, and time.

It doesn’t just remember — it keeps the agent’s **ability to execute** alive.

### Key Capabilities

- **Project Bootstrap** — Instantly inject deep understanding into any existing repository (no more cold starts)
- **Evolving Task Intelligence** — Automatically captures and refines requirements as your conversation evolves
- **Operational Tray** — Dynamic workspace that gives the agent exactly what it needs right now (tools, rules, context, artifacts)
- **Interruption-Resilient Continuity** — Smart checkpoints let agents resume exactly where they left off
- **Multi-Agent & Multi-Model Support** — Seamless handoff between Claude, Codex, GLM, and others
- **Cross-Machine Continuity** — Work across Windows and Linux with shared operational knowledge

### Real Workflows That Just Work

```text
Claude plans the architecture
   ↓
Codex implements it efficiently
   ↓
GLM continues after Claude limits expire
   ↓
Claude returns for final review

→ No recap needed. Full continuity preserved.
```

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

### Option 1: First-User Docker Compose

Generate a local `.env` file and start the non-dev Compose stack:

```bash
python scripts/configure_public.py --non-interactive
docker compose --env-file .env.user -f docker-compose.user.yml up -d
```

This path uses the published Docker Hub image and a named Qdrant volume. It does
not mount the source tree into the container, and it keeps user runtime settings
in `.env.user` instead of the contributor `.env`.

Health check:

```bash
curl http://localhost:8000/api/v1/health
```

The current public image is published as `caveboy/mnemoforge:latest`.
Immutable commit tags are published alongside `latest` as
`caveboy/mnemoforge:<git-sha>`.

### Option 2: Docker Compose For Contributors

```bash
git clone https://github.com/Utundry/mnemoforge.git
cd mnemoforge
docker compose up -d
```

The contributor stack builds from source and also starts a dev container with
live mounts.

Default contributor ports:

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

- prefer `docker-compose.user.yml` for a simple non-dev runtime;
- generate `.env.user` with `python scripts/configure_public.py`;
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
- [docs/I18N_POLICY.md](docs/I18N_POLICY.md): documentation language policy
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

MnemoForge is created and maintained by Codex as the programmer, MnemoForge as the taskmaster, and Nikolay Laptev as the questioner.

- Email: `caveboy@yandex.ru`
- Docker Hub: `caveboy/mnemoforge`
- GitHub repository: `Utundry/mnemoforge`

## Project Name

`MnemoForge` is the public release name. The internal project id remains
`mnemoforge` in task history, storage metadata, and compatibility paths.

P.S. НакУй проект c КузницейПамяти!
