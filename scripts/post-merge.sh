#!/bin/bash
set -e
pnpm install --frozen-lockfile
# NOTE: Do NOT run `pnpm --filter db push` (drizzle-kit push) here.
# The lib/db Drizzle schema is a partial view of the database only.
# The full schema is managed exclusively by Alembic (Python migrations).
# Running drizzle push would attempt to DROP ~20 tables and 13+ columns of live data.
# Apply any pending Python/Alembic DB migrations.
cd backend-py && PYTHONPATH=. python -m alembic upgrade head
