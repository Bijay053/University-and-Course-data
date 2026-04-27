#!/bin/bash
set -e
pnpm install --frozen-lockfile
pnpm --filter db push || true
# Apply any pending Python/Alembic DB migrations.
cd backend-py && PYTHONPATH=. python -m alembic upgrade head
