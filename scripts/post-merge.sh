#!/bin/bash
set -e
pnpm install --frozen-lockfile
# `pnpm --filter db push` is a no-op when no `db` workspace package exists;
# the `|| true` prevents set -e from aborting the script in that case.
# If a `db` package is ever added this will execute normally.
pnpm --filter db push || true
# Apply any pending Python/Alembic DB migrations.
cd backend-py && PYTHONPATH=. python -m alembic upgrade head
