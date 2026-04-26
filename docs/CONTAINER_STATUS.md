# Container Orchestration Status

This project currently runs two API services in Docker Compose:

- `memory-server-dev` (live mounts, fast iteration): host `8000` -> container `8000`
- `memory-server` (baked production image): host `8001` -> container `8000`
- `qdrant`: host `6333/6334`

The Compose `test` profile adds isolated, disposable services for remote MCP e2e:

- `memory-server-test`: host `8010` -> container `8000`
- `qdrant-test`: no host port; Docker-network only
- `mcp-e2e-test-runner`: one-shot external client over HTTP/MCP

`memory-server-test` and `qdrant-test` use tmpfs-backed storage and test-only
collections/API key, so replay fixtures and synthetic e2e data do not enter the
working `qdrant_data` directory.

## Operational intent

- Use `memory-server-dev` for local development and rapid code edits.
- Use `memory-server` to verify production-image behavior.
- Keep only one API service active if you want a single stable endpoint for clients.

## Common commands

```powershell
docker compose ps
docker compose logs memory-server-dev --tail 80
docker compose logs memory-server --tail 80
docker compose port memory-server 8000
docker compose port memory-server-dev 8000
```

## Isolated remote MCP e2e

Use this before trusting replay/checkpoint changes that are meant for remote MCP
clients:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_docker_remote_mcp_e2e.ps1
```

The direct Compose form is:

```powershell
docker compose --profile test up --build --abort-on-container-exit --exit-code-from mcp-e2e-test-runner mcp-e2e-test-runner
```

The wrapper validates Compose config, warns if host Ollama is unreachable, runs
the remote MCP replay probe, and stops test-only services afterward. Use
`-KeepServices` to inspect test containers after a failure.

## Production image refresh

Use:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update_production.ps1
```

The script rebuilds `memory-server`, recreates the container, and prints the published host endpoint for container port `8000` (instead of assuming a fixed host port).
