#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[setup_wsl] project root: $ROOT_DIR"

echo "[setup_wsl] installing system packages"
sudo apt update
sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  python3-dev \
  build-essential \
  git \
  curl \
  ripgrep \
  sqlite3 \
  pkg-config \
  libffi-dev \
  libssl-dev

echo "[setup_wsl] creating Linux virtual environment at .venv-wsl"
python3 -m venv .venv-wsl

echo "[setup_wsl] activating virtual environment"
source .venv-wsl/bin/activate

echo "[setup_wsl] upgrading pip toolchain"
python -m pip install --upgrade pip setuptools wheel

echo "[setup_wsl] installing project requirements"
pip install -r requirements.txt

echo "[setup_wsl] installing pytest"
pip install pytest

echo "[setup_wsl] verifying Python environment"
python --version
pytest --version
python - <<'PY'
import fastapi
import httpx
import pydantic

print("python deps ok")
PY

echo "[setup_wsl] done"
echo "[setup_wsl] next steps:"
echo "  source .venv-wsl/bin/activate"
echo "  pytest"
echo "  docker compose up -d"
echo "  ./scripts/check_docker_server.sh"
