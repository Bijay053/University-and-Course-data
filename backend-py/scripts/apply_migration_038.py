"""Migration 038 — University regression alerts table.

Creates:
  - university_regression_alerts: stores detected regressions with severity,
    probable causes, and resolution status.

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_038.py

Apply on prod:
    cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_038.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS university_regression_alerts (
    id               SERIAL PRIMARY KEY,
    university_id    INTEGER      NOT NULL,
    job_id           TEXT,
    alert_type       TEXT         NOT NULL,
    severity         TEXT         NOT NULL,
    previous_value   NUMERIC,
    current_value    NUMERIC,
    delta            NUMERIC,
    probable_causes  JSONB        NOT NULL DEFAULT '[]',
    status           TEXT         NOT NULL DEFAULT 'open',
    snapshot_date    DATE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    acknowledged_at  TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ,
    CONSTRAINT chk_ura_severity CHECK (severity IN ('critical', 'high', 'medium')),
    CONSTRAINT chk_ura_status   CHECK (status   IN ('open', 'acknowledged', 'resolved'))
)
"""

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_ura_uni_status
    ON university_regression_alerts (university_id, status, created_at DESC)
"""

DEDUP_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_ura_uni_type_date
    ON university_regression_alerts (university_id, alert_type, snapshot_date)
    WHERE status != 'resolved'
"""


async def run() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text(TABLE_DDL))
        await db.execute(text(INDEX_DDL))
        await db.execute(text(DEDUP_INDEX_DDL))
        await db.commit()

        r = await db.execute(text("SELECT COUNT(*) FROM university_regression_alerts"))
        print(f"Migration 038 applied — university_regression_alerts rows: {r.scalar()}")


if __name__ == "__main__":
    asyncio.run(run())
