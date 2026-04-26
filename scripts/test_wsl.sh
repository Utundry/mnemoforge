#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".venv-wsl/bin/activate" ]]; then
  echo "[test_wsl] missing .venv-wsl/bin/activate"
  echo "[test_wsl] run ./scripts/setup_wsl.sh first"
  exit 1
fi

source .venv-wsl/bin/activate

echo "[test_wsl] python: $(command -v python)"
echo "[test_wsl] pytest: $(command -v pytest)"

if [[ $# -gt 0 ]]; then
  echo "[test_wsl] running: pytest $*"
  pytest "$@"
else
  echo "[test_wsl] running full test suite"
  pytest
fi
