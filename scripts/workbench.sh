#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -z "${LLM_CONFIG_FILE:-}" ]]; then
  if [[ -f "$ROOT/config/llm.local.json" ]]; then
    export LLM_CONFIG_FILE="$ROOT/config/llm.local.json"
  else
    export LLM_CONFIG_FILE="$ROOT/config/llm_config.json"
  fi
fi
./scripts/deploy.sh start
echo "Workbench: http://127.0.0.1:${API_PORT:-8000}"
echo "Starting trusted local worker. Keep this terminal open; Ctrl-C stops the worker."
export PYTHONDONTWRITEBYTECODE=1
exec "${RD_CONTROL_PYTHON:-$ROOT/.venv-control/bin/python}" scripts/workbench_worker.py
