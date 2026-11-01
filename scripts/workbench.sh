#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
./scripts/deploy.sh start
echo "Workbench: http://127.0.0.1:${API_PORT:-8000}"
echo "Starting trusted local worker. Keep this terminal open; Ctrl-C stops the worker."
export PYTHONDONTWRITEBYTECODE=1
exec "${RD_CONTROL_PYTHON:-$ROOT/.venv-control/bin/python}" scripts/workbench_worker.py
