"""Apply migration 017 (alert dashboard views) directly via asyncpg.

Per replit.md, alembic does not work in this env (DNS/SSL issue with
asyncpg + localhost).  This script extracts the CREATE OR REPLACE VIEW
statements from migration 017 and runs them via the configured
DATABASE_URL.  Idempotent — safe to re-run.

Usage:
    cd backend-py && PYTHONPATH=. python scripts/apply_migration_017.py
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database import AsyncSessionLocal, engine


async def _apply() -> None:
    # Inline-import the SQL constants from the migration module so we
    # don't duplicate the DDL.
    import importlib.util
    import pathlib
    mig_path = pathlib.Path(__file__).resolve().parent.parent / (
        "alembic/versions/017_alert_dashboard_views.py"
    )
    spec = importlib.util.spec_from_file_location("mig017", mig_path)
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)  # type: ignore[union-attr]

    async with AsyncSessionLocal() as db:
        await db.execute(text(mig._V_ACTIVE_ALERTS_SQL))
        await db.execute(text(mig._V_UNIVERSITY_HEALTH_SQL))
        await db.commit()
    await engine.dispose()
    print("Migration 017 applied: v_active_alerts + v_university_health created")


if __name__ == "__main__":
    asyncio.run(_apply())
