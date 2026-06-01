"""Migration 035 — Create bulk_repair_history audit log table.

Adds:
  - bulk_repair_history: records every bulk repair action with user, outcome, risk counts

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_035.py

Apply on prod:
    cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_035.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal


TABLE_DDL = """
CREATE TABLE IF NOT EXISTS bulk_repair_history (
    id                  SERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    triggered_by_email  TEXT        NOT NULL,
    triggered_by_name   TEXT,
    issue_types         TEXT[]      NOT NULL DEFAULT '{}',
    selected_count      INTEGER     NOT NULL DEFAULT 0,
    queued_count        INTEGER     NOT NULL DEFAULT 0,
    skipped_count       INTEGER     NOT NULL DEFAULT 0,
    failed_count        INTEGER     NOT NULL DEFAULT 0,
    mark_testing        BOOLEAN     NOT NULL DEFAULT FALSE,
    university_names    TEXT[]      NOT NULL DEFAULT '{}',
    result              JSONB
)
"""

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_bulk_repair_history_created_at
    ON bulk_repair_history (created_at DESC)
"""


async def run() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text(TABLE_DDL))
        await db.execute(text(INDEX_DDL))
        await db.commit()
        print("Migration 035 applied — bulk_repair_history table created.")


if __name__ == "__main__":
    asyncio.run(run())
