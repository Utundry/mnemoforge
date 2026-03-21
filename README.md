# Supermemory

Local semantic memory server for AI agents.

**Stack:** FastAPI + Qdrant (vector DB) + Ollama (embeddings + local LLM).

## Quick start

- Server setup: `SETUP.md`
- Client (remote) setup: `CLIENT_SETUP.md`

### Docker Compose

```bash
docker compose up -d
```

Default ports:
- Qdrant: `6333` / `6334`
- API: `8000`

Health:
```bash
curl http://localhost:8000/api/v1/health
```

## MCP

Two transports are available:

- **SSE** (zero-config client):
  - `.mcp.json` points to `http://localhost:8000/mcp/sse`
- **STDIO** (local process):
  - `python -m mcp.server` (expects `MEMORY_SERVER_URL`, default `http://localhost:8000`)

## Security (recommended when not purely local)

Set these in `.env`:

- `API_KEY` — require `X-Api-Key: <value>` header for all non-exempt endpoints.
- `INGEST_ALLOWED_ROOTS` — comma-separated directories that filesystem-reading endpoints may access.
  - Applies to project ingestion and `/api/v1/ingest/*` routes.
- `MAX_REQUEST_SIZE_MB` — reject large request bodies (0 = unlimited).
- `LLM_RATE_LIMIT_PER_MIN` — per-IP limit for LLM-heavy endpoints (0 = unlimited).

If you expose the server on a network interface (`SERVER_HOST=0.0.0.0`), enable `API_KEY` at minimum.

