"""Migration 044 — add category column to agent_recovery_results.

Records which budget category (e.g. 'fees' or 'english') each recovery
result or trace row was produced under.  Operators can query this column
to debug budget exhaustion for a specific PDF type without having to
parse source_type strings.

Run once per environment:
  PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_044.py
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATEMENTS = [
    """
    ALTER TABLE agent_recovery_results
        ADD COLUMN IF NOT EXISTS category TEXT
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_agent_recovery_category
        ON agent_recovery_results (category)
        WHERE category IS NOT NULL
    """,
]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info(
        "Migration 044 applied — category column added to agent_recovery_results"
    )


if __name__ == "__main__":
    asyncio.run(main())
