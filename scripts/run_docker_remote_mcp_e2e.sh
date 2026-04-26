#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

NO_BUILD="${NO_BUILD:-0}"
SKIP_OLLAMA_PREFLIGHT="${SKIP_OLLAMA_PREFLIGHT:-0}"
KEEP_SERVICES="${KEEP_SERVICES:-0}"

echo "[docker-remote-mcp-e2e] project root: $ROOT_DIR"
echo "[docker-remote-mcp-e2e] checking docker compose config"
docker compose --profile test config --quiet

if [[ "$SKIP_OLLAMA_PREFLIGHT" != "1" ]]; then
  echo "[docker-remote-mcp-e2e] checking host Ollama at http://localhost:11434/api/tags"
  if curl --fail --silent --show-error --max-time 5 http://localhost:11434/api/tags >/dev/null; then
    echo "[docker-remote-mcp-e2e] host Ollama reachable"
  else
    echo "[docker-remote-mcp-e2e] WARNING: host Ollama is not reachable. The e2e may fail when embeddings are needed." >&2
  fi
fi

args=(compose --profile test up --abort-on-container-exit --exit-code-from mcp-e2e-test-runner)
if [[ "$NO_BUILD" != "1" ]]; then
  args+=(--build)
fi
args+=(mcp-e2e-test-runner)

cleanup() {
  if [[ "$KEEP_SERVICES" != "1" ]]; then
    echo "[docker-remote-mcp-e2e] stopping test-only services"
    docker compose --profile test stop mcp-e2e-test-runner memory-server-test qdrant-test
  else
    echo "[docker-remote-mcp-e2e] keeping test services running for inspection"
  fi
}
trap cleanup EXIT

echo "[docker-remote-mcp-e2e] running: docker ${args[*]}"
docker "${args[@]}"
