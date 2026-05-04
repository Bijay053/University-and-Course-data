#!/usr/bin/env bash
# Startup wrapper for the FastAPI / uvicorn process.
#
# Problem: PM2 inherits a system-level placeholder DATABASE_URL
# (postgres://user:password@host:5432/dbname) which pydantic-settings
# prefers over the .env file, breaking every DB query.
#
# Fix: unset the system-level overrides, then source .env so the real
# production values take effect before uvicorn is exec'd.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# Clear variables that the system env may carry as placeholders
unset DATABASE_URL REDIS_URL GEMINI_API_KEY SESSION_SECRET CELERY_BROKER_URL 2>/dev/null || true

# Export everything in .env into this shell's environment
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

exec "$SCRIPT_DIR/venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8080 --workers 1
