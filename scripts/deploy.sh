#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ACTION="${1:-status}"
case "$ACTION" in
  start|stop|status|logs) ;;
  *) echo "Usage: $0 {start|stop|status|logs}" >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then
  echo "Usage: $0 {start|stop|status|logs}" >&2
  exit 2
fi

command -v docker >/dev/null || { echo "Docker is required. Start Docker Desktop first." >&2; exit 1; }
# An isolated empty configuration avoids reading user registry credentials.
export DOCKER_CONFIG="$ROOT/sandbox-validation/service/docker-config"
mkdir -p "$DOCKER_CONFIG"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null && docker-compose version >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose v2 (with up --wait) is required." >&2
  exit 1
fi
docker info >/dev/null 2>&1 || { echo "Docker daemon is unavailable. Start Docker Desktop." >&2; exit 1; }
export API_PORT="${API_PORT:-8000}" PROBE_PORT="${PROBE_PORT:-8001}"
export SERVICE_UID="${SERVICE_UID:-$(id -u)}" SERVICE_GID="${SERVICE_GID:-$(id -g)}"
export RD_SERVICE_PROJECT="${RD_SERVICE_PROJECT:-rd-platform-service}"
COMPOSE+=(--project-name "$RD_SERVICE_PROJECT" -f "$ROOT/docker-compose.service.yml")

case "$ACTION" in
  start)
    CONTROL_PYTHON="${RD_CONTROL_PYTHON:-$ROOT/.venv-control/bin/python}"
    if [[ ! -x "$CONTROL_PYTHON" ]]; then
      echo "Missing control virtualenv: $CONTROL_PYTHON" >&2
      echo "Create .venv-control and install docker==7.1.0, or set RD_CONTROL_PYTHON." >&2
      exit 1
    fi
    export PYTHONDONTWRITEBYTECODE=1
    "$CONTROL_PYTHON" scripts/build_service_image.py --check-ports
    mkdir -p data logs models
    "${COMPOSE[@]}" config --quiet
    "$CONTROL_PYTHON" scripts/build_service_image.py
    if ! "${COMPOSE[@]}" up -d --no-build --wait --wait-timeout 90; then
      "${COMPOSE[@]}" logs --tail 100 >&2 || true
      echo "Startup failed. Inspect logs or run ./scripts/deploy.sh stop." >&2
      exit 1
    fi
    "$CONTROL_PYTHON" scripts/build_service_image.py --check-health
    "${COMPOSE[@]}" ps
    "${COMPOSE[@]}" logs --tail 30
    echo "API: http://127.0.0.1:$API_PORT/docs"
    echo "Probe: http://127.0.0.1:$PROBE_PORT/probe"
    echo "Verify: $CONTROL_PYTHON examples/client_demo.py --api-url http://127.0.0.1:$API_PORT --probe-url http://127.0.0.1:$PROBE_PORT"
    ;;
  stop) "${COMPOSE[@]}" down --remove-orphans ;;
  status) "${COMPOSE[@]}" ps --all ;;
  logs) "${COMPOSE[@]}" logs --tail 100 --follow ;;
esac
