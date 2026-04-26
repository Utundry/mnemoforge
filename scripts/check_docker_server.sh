#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
MCP_BASE="${MCP_BASE:-http://localhost:8000/mcp}"

echo "[check_docker_server] project root: $ROOT_DIR"
echo "[check_docker_server] docker compose status"
docker compose ps

echo "[check_docker_server] API health: $API_BASE/health"
curl --fail --silent --show-error "$API_BASE/health"
echo

echo "[check_docker_server] MCP SSE endpoint headers: $MCP_BASE/sse"
curl --fail --silent --show-error -I "$MCP_BASE/sse"
echo

echo "[check_docker_server] done"
