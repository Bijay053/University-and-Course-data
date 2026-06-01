"""Migration 034 — Add certification_status workflow columns to universities.

Adds:
  - certification_status TEXT  DEFAULT 'draft'
  - last_certified_score INTEGER
  - last_certified_at TIMESTAMP WITH TIME ZONE

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_034.py

Apply on prod:
    cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_034.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal

STATEMENTS = [
    "ALTER TABLE universities ADD COLUMN IF NOT EXISTS certification_status TEXT NOT NULL DEFAULT 'draft'",
    "ALTER TABLE universities ADD COLUMN IF NOT EXISTS last_certified_score INTEGER",
    "ALTER TABLE universities ADD COLUMN IF NOT EXISTS last_certified_at TIMESTAMP WITH TIME ZONE",
    """
    DO $do$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'universities_certification_status_check'
        ) THEN
            ALTER TABLE universities
                ADD CONSTRAINT universities_certification_status_check
                CHECK (certification_status IN ('draft','testing','certified','needs_review','failed'));
        END IF;
    END
    $do$
    """,
]


async def main():
    async with AsyncSessionLocal() as db:
        for stmt in STATEMENTS:
            await db.execute(text(stmt))
        await db.commit()
    print("Migration 034 applied — certification_status columns added to universities.")


if __name__ == "__main__":
    asyncio.run(main())
