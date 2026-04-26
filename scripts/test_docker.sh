#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SERVICE="${DOCKER_TEST_SERVICE:-memory-server-dev}"

echo "[test_docker] project root: $ROOT_DIR"
echo "[test_docker] service: $SERVICE"

if [[ $# -gt 0 ]]; then
  echo "[test_docker] running in container: python -m pytest $*"
  docker compose run --rm "$SERVICE" python -m pytest "$@"
else
  echo "[test_docker] running full test suite in container"
  docker compose run --rm "$SERVICE" python -m pytest
fi
