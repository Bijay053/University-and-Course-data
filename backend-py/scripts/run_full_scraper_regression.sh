#!/usr/bin/env bash
set -euo pipefail

REDIS_HOST="${TEST_REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${TEST_REDIS_PORT:-6379}"
STARTED_REDIS=0
PYTEST_PID=""

redis_ready() {
  redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -qx PONG
}

cleanup() {
  if [[ "$STARTED_REDIS" -eq 1 ]]; then
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" shutdown nosave >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

forward_signal() {
  local signal_name="$1"
  local exit_code="$2"

  trap - "$signal_name"
  if [[ -n "$PYTEST_PID" ]] && kill -0 "$PYTEST_PID" 2>/dev/null; then
    kill -s "$signal_name" "$PYTEST_PID" 2>/dev/null || true
  fi
  if [[ -n "$PYTEST_PID" ]]; then
    wait "$PYTEST_PID" 2>/dev/null || true
  fi
  exit "$exit_code"
}

trap 'forward_signal HUP 129' HUP
trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM

if ! command -v redis-cli >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1; then
  echo "ERROR: full scraper regression requires redis-cli and redis-server." >&2
  exit 2
fi

if ! redis_ready; then
  echo "Redis is unavailable at ${REDIS_HOST}:${REDIS_PORT}; starting a temporary test instance..."
  redis-server \
    --bind "$REDIS_HOST" \
    --port "$REDIS_PORT" \
    --save "" \
    --appendonly no \
    --daemonize yes \
    --loglevel warning
  STARTED_REDIS=1

  for _ in {1..50}; do
    if redis_ready; then
      break
    fi
    sleep 0.1
  done
fi

if ! redis_ready; then
  echo "ERROR: Redis did not become ready at ${REDIS_HOST}:${REDIS_PORT}; scraper tests were not started." >&2
  exit 2
fi

echo "Redis dependency ready at ${REDIS_HOST}:${REDIS_PORT}."
PYTHONPATH=. python -m pytest -q "$@" &
PYTEST_PID=$!
wait "$PYTEST_PID"