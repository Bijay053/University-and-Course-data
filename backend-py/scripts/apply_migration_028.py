#!/usr/bin/env python3
"""Migration 028 — Phase 9B: extend conflict_repair_log with resolution metadata.

Adds:
  - resolved_by          JSONB   — list of source types that agreed on the resolution
  - resolution_confidence INTEGER — 0-100, how confident we are in the repair
  - resolution_method    VARCHAR — "drop_low_authority" | "normalization_equivalence"
                                    | "source_revalidation" | "unresolved"

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_028.py
"""
import asyncio
import os

_raw_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@127.0.0.1:5432/university_portal",
)
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_STMTS = [
    "ALTER TABLE conflict_repair_log ADD COLUMN IF NOT EXISTS resolved_by JSONB",
    "ALTER TABLE conflict_repair_log ADD COLUMN IF NOT EXISTS resolution_confidence INTEGER",
    "ALTER TABLE conflict_repair_log ADD COLUMN IF NOT EXISTS resolution_method VARCHAR(60)",
    """
    COMMENT ON COLUMN conflict_repair_log.resolved_by IS
        'Source types that agreed on the resolution, e.g. [\"html\", \"pdf\"]'
    """,
    """
    COMMENT ON COLUMN conflict_repair_log.resolution_confidence IS
        '0-100 confidence score for the resolution decision'
    """,
    """
    COMMENT ON COLUMN conflict_repair_log.resolution_method IS
        'How the conflict was resolved: drop_low_authority | normalization_equivalence '
        '| source_revalidation | unresolved'
    """,
    # Back-fill existing rows
    """
    UPDATE conflict_repair_log
       SET resolution_method = CASE
               WHEN resolved AND action_taken = 'drop_low_authority'
                   THEN 'drop_low_authority'
               WHEN NOT resolved
                   THEN 'unresolved'
               ELSE action_taken
           END
     WHERE resolution_method IS NULL
    """,
]


async def run() -> None:
    from app.database import engine
    from sqlalchemy import text

    async with engine.begin() as conn:
        for stmt in _STMTS:
            await conn.execute(text(stmt))
    print("Migration 028 applied: conflict_repair_log extended with resolution metadata.")


if __name__ == "__main__":
    asyncio.run(run())
