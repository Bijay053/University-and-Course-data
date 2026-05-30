#!/usr/bin/env python3
"""Migration — Add probe_result, probe_status, probe_updated_at to universities.

Safe to run multiple times (IF NOT EXISTS / coalesce guards).

asyncpg requires each statement to be executed individually — multi-statement
text() blocks cause "cannot insert multiple commands into a prepared statement".

Run on dev:
  cd backend-py && PYTHONPATH=. python scripts/apply_migration_probe_columns.py

Run on prod:
  cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_probe_columns.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal

# Each statement must be executed individually with asyncpg
STATEMENTS = [
    # Probe result: full SiteProfile dict from site_probe.probe_site()
    "ALTER TABLE universities ADD COLUMN IF NOT EXISTS probe_result JSONB",
    # Probe status: none | probing | configured | failed
    "ALTER TABLE universities ADD COLUMN IF NOT EXISTS probe_status TEXT NOT NULL DEFAULT 'none'",
    # Timestamp of last probe run
    "ALTER TABLE universities ADD COLUMN IF NOT EXISTS probe_updated_at TIMESTAMPTZ",
    # Partial index to quickly find unis that still need probing
    """CREATE INDEX IF NOT EXISTS ix_universities_needs_probe
       ON universities (id) WHERE probe_status = 'none'""",
]


async def main() -> None:
    print("Applying probe columns migration on universities ...")
    async with AsyncSessionLocal() as db:
        for stmt in STATEMENTS:
            await db.execute(text(stmt))
            print(f"  OK: {stmt[:70].strip()}...")
        await db.commit()
    print("Done — probe_result, probe_status, probe_updated_at added (or already existed).")


if __name__ == "__main__":
    asyncio.run(main())
