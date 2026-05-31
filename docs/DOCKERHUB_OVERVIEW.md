# SloplessCode

Operational continuity infrastructure for AI coding agents.

SloplessCode helps AI coding agents continue real project work across IDE
restarts, model switches, context loss, interrupted sessions, and multi-agent
handoffs. It stores project state, task lifecycle, checkpoints, governed rules,
decisions, and searchable memory behind HTTP and MCP interfaces.

Formerly Mnemoforge. During the rename transition, the same image is also
published as `caveboy/mnemoforge:latest` for compatibility.

## What It Is

SloplessCode is not just chat memory. It is an operational runtime for agent-led
software development:

- project memory and semantic search
- task and improvement lifecycle
- checkpoints and handoff notes
- project laws/rules and operational guidance
- MCP tools for coding agents and local models
- backup/restore and storage-health workflows

The goal is simple: an agent should not start from zero every time a session is
reset. It should be able to ask the system what happened, what matters, and what
the next safe action is.

## Quick Start

The recommended user path is Docker Compose from the repository:

```powershell
git clone https://github.com/Utundry/sloplesscode.git
cd sloplesscode
python scripts/configure_public.py --non-interactive
docker compose --env-file .env.user -f docker-compose.user.yml up -d
```

Health check:

```powershell
curl http://localhost:8000/api/v1/health
```

MCP SSE endpoint:

```text
http://localhost:8000/mcp/sse
```

## Image Tags

Current image:

```powershell
docker pull caveboy/sloplesscode:latest
```

Compatibility image during the rename transition:

```powershell
docker pull caveboy/mnemoforge:latest
```

Immutable commit tags are published alongside `latest` when release publishing
uses `--tag-current-git-sha`.

## Data Persistence

Container updates should not delete your project data if you keep the Compose
volumes/directories intact.

Do not run destructive volume cleanup commands unless you have a backup:

- avoid `docker compose down -v`
- avoid `docker system prune --volumes`
- do not delete the local data directory or named volume

The historical data directory name is `qdrant_data`, but it now contains the
whole SloplessCode system data root, not only Qdrant vector data. Treat it as
your project/system memory.

## Alpha Status

SloplessCode is alpha software under active architectural evolution. MCP
behaviors, workflows, and storage surfaces may change. Public images are tested
through Docker-backed checks and live MCP smoke checks before publishing, but
you should still keep backups and report regressions.

## Useful Links

- GitHub: `https://github.com/Utundry/sloplesscode`
- Docker Hub: `caveboy/sloplesscode`
- Compatibility Docker Hub image: `caveboy/mnemoforge`
- Maintainer: Nikolay Laptev, `caveboy@yandex.ru`

