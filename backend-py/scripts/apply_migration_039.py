"""Migration 039 — Auto-repair suggestions table.

Creates:
  - auto_repair_suggestions: stores AI-diagnosed fix suggestions with validation results.

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_039.py

Apply on prod:
    cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_039.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS auto_repair_suggestions (
    id                    SERIAL PRIMARY KEY,
    university_id         INTEGER      NOT NULL,
    regression_alert_id   INTEGER      REFERENCES university_regression_alerts(id) ON DELETE SET NULL,

    -- AI Diagnosis
    issue_summary         TEXT,
    root_cause_category   TEXT,
    fix_recommendation    TEXT,
    fix_yaml_snippet      TEXT,
    safe_fix              JSONB,
    risk_label            TEXT,
    developer_note        TEXT,
    evidence              JSONB        NOT NULL DEFAULT '[]',

    -- Validation
    validation_result     JSONB,
    confidence            TEXT,

    -- Status lifecycle
    status                TEXT         NOT NULL DEFAULT 'pending',

    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    applied_at            TIMESTAMPTZ,
    dismissed_at          TIMESTAMPTZ,

    CONSTRAINT chk_ars_status CHECK (status IN (
        'pending', 'ready', 'developer_required', 'applied', 'dismissed', 'failed'
    )),
    CONSTRAINT chk_ars_confidence CHECK (confidence IS NULL OR confidence IN ('high', 'medium', 'low')),
    CONSTRAINT chk_ars_risk_label CHECK (risk_label IS NULL OR risk_label IN ('low', 'medium', 'developer_required'))
)
"""

INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_ars_uni_status ON auto_repair_suggestions (university_id, status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ars_alert      ON auto_repair_suggestions (regression_alert_id) WHERE regression_alert_id IS NOT NULL",
]


async def run() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text(TABLE_DDL))
        for idx in INDEXES_DDL:
            await db.execute(text(idx))
        await db.commit()

        r = await db.execute(text("SELECT COUNT(*) FROM auto_repair_suggestions"))
        print(f"Migration 039 applied — auto_repair_suggestions rows: {r.scalar()}")


if __name__ == "__main__":
    asyncio.run(run())
