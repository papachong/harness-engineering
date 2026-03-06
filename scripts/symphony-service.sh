#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_WORKFLOW_PATH="$ROOT_DIR/WORKFLOW.md"
DEFAULT_RUNTIME_DIR="$ROOT_DIR/.symphony_runtime"
DEFAULT_PID_FILE="$DEFAULT_RUNTIME_DIR/symphony.pid"
DEFAULT_LOG_FILE="$DEFAULT_RUNTIME_DIR/symphony.log"
DEFAULT_PORT="${SYMPHONY_PORT:-8080}"
DEFAULT_HOST="${SYMPHONY_HOST:-127.0.0.1}"
DEFAULT_ENV_FILE="${SYMPHONY_ENV_FILE:-$ROOT_DIR/.env.symphony}"

WORKFLOW_PATH="${SYMPHONY_WORKFLOW_PATH:-$DEFAULT_WORKFLOW_PATH}"
RUNTIME_DIR="${SYMPHONY_RUNTIME_DIR:-$DEFAULT_RUNTIME_DIR}"
PID_FILE="${SYMPHONY_PID_FILE:-$DEFAULT_PID_FILE}"
LOG_FILE="${SYMPHONY_LOG_FILE:-$DEFAULT_LOG_FILE}"
PORT="$DEFAULT_PORT"
HOST="$DEFAULT_HOST"

usage() {
  cat <<EOF
Usage: $(basename "$0") <start|stop|restart|status> [--port PORT] [--workflow PATH] [--log-file PATH] [--pid-file PATH]

Environment overrides:
  SYMPHONY_PORT           Service port (default: 8080)
  SYMPHONY_HOST           Status check host (default: 127.0.0.1)
  SYMPHONY_WORKFLOW_PATH  Workflow file path (default: WORKFLOW.md)
  SYMPHONY_RUNTIME_DIR    Runtime directory (default: .symphony_runtime)
  SYMPHONY_PID_FILE       PID file path
  SYMPHONY_LOG_FILE       Log file path
  SYMPHONY_ENV_FILE       Optional env file to source before start (default: .env.symphony)

Examples:
  $(basename "$0") start
  $(basename "$0") status --port 8080
  $(basename "$0") restart --workflow ./WORKFLOW.md
EOF
}

log() {
  printf '[symphony-service] %s\n' "$*"
}

fail() {
  printf '[symphony-service] %s\n' "$*" >&2
  exit 1
}

process_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  if [[ -f "$PID_FILE" ]]; then
    tr -d '[:space:]' <"$PID_FILE"
  fi
}

ensure_runtime_dir() {
  mkdir -p "$RUNTIME_DIR"
}

find_port_pid() {
  lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1
}

load_env_file() {
  if [[ -f "$DEFAULT_ENV_FILE" ]]; then
    log "loading environment from $DEFAULT_ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$DEFAULT_ENV_FILE"
    set +a
  fi
}

wait_for_http() {
  local attempt
  for attempt in {1..20}; do
    if curl -fsS --max-time 2 "http://$HOST:$PORT/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_service() {
  ensure_runtime_dir

  local existing_pid
  existing_pid="$(read_pid || true)"
  if process_running "$existing_pid"; then
    log "service already running with pid $existing_pid"
    return 0
  fi

  local port_pid
  port_pid="$(find_port_pid || true)"
  if [[ -n "$port_pid" ]]; then
    fail "port $PORT is already in use by pid $port_pid"
  fi

  [[ -f "$WORKFLOW_PATH" ]] || fail "workflow file not found: $WORKFLOW_PATH"

  local python_bin="$ROOT_DIR/.venv/bin/python"
  if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3 || true)"
  fi
  [[ -n "$python_bin" ]] || fail "python3 not found"

  load_env_file

  if [[ -z "${LINEAR_API_KEY:-}" ]]; then
    fail "LINEAR_API_KEY is not set"
  fi

  log "starting service on port $PORT"
  (
    cd "$ROOT_DIR"
    export PYTHONPATH="${PYTHONPATH:-src}"
    nohup "$python_bin" -m symphony.cli "$WORKFLOW_PATH" --port "$PORT" >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
  )

  local new_pid
  new_pid="$(read_pid || true)"
  if [[ -z "$new_pid" ]]; then
    fail "failed to record service pid"
  fi

  if wait_for_http; then
    log "service started with pid $new_pid"
    log "log file: $LOG_FILE"
    return 0
  fi

  if ! process_running "$new_pid"; then
    rm -f "$PID_FILE"
  fi
  fail "service did not become ready; check $LOG_FILE"
}

stop_service() {
  local pid
  pid="$(read_pid || true)"

  if [[ -z "$pid" ]]; then
    pid="$(find_port_pid || true)"
  fi

  if [[ -z "$pid" ]]; then
    log "service is not running"
    rm -f "$PID_FILE"
    return 0
  fi

  if ! process_running "$pid"; then
    log "stale pid file found; cleaning up"
    rm -f "$PID_FILE"
    return 0
  fi

  log "stopping service pid $pid"
  kill "$pid" 2>/dev/null || true

  local _
  for _ in {1..10}; do
    if ! process_running "$pid"; then
      rm -f "$PID_FILE"
      log "service stopped"
      return 0
    fi
    sleep 1
  done

  log "forcing service shutdown for pid $pid"
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  log "service stopped"
  return 0
}

status_service() {
  local pid
  pid="$(read_pid || true)"

  if process_running "$pid"; then
    log "service is running with pid $pid on port $PORT"
    exit 0
  fi

  local port_pid
  port_pid="$(find_port_pid || true)"
  if [[ -n "$port_pid" ]]; then
    log "service appears to be running on port $PORT with pid $port_pid"
    exit 0
  fi

  log "service is not running"
  exit 1
}

ACTION="${1:-}"
if [[ -z "$ACTION" ]]; then
  usage
  exit 1
fi
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    --workflow)
      WORKFLOW_PATH="$2"
      shift 2
      ;;
    --log-file)
      LOG_FILE="$2"
      shift 2
      ;;
    --pid-file)
      PID_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

case "$ACTION" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  status)
    status_service
    ;;
  *)
    usage
    exit 1
    ;;
esac