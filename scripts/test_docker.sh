#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SERVICE="${DOCKER_TEST_SERVICE:-mcp-e2e-test-runner}"
NO_BUILD="${NO_BUILD:-0}"

echo "[test_docker] project root: $ROOT_DIR"
echo "[test_docker] service: $SERVICE"

docker compose --profile test config --quiet

if [[ "$NO_BUILD" != "1" ]]; then
  echo "[test_docker] building test runner image"
  docker compose --profile test build "$SERVICE"
fi

if [[ $# -gt 0 ]]; then
  echo "[test_docker] running in container: python -m pytest $*"
  docker compose --profile test run --rm "$SERVICE" python -m pytest "$@"
else
  echo "[test_docker] running full test suite in container"
  docker compose --profile test run --rm "$SERVICE" python -m pytest
fi
