"""Apply migration 018 (CRICOS coverage view) directly via asyncpg.

Per replit.md, alembic does not work in this env.  Idempotent — safe
to re-run.  Usage:

    cd backend-py && PYTHONPATH=. python scripts/apply_migration_018.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib

from sqlalchemy import text

from app.database import AsyncSessionLocal, engine


async def _apply() -> None:
    mig_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "alembic/versions/018_cricos_coverage_view.py"
    )
    spec = importlib.util.spec_from_file_location("mig018", mig_path)
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)  # type: ignore[union-attr]

    async with AsyncSessionLocal() as db:
        await db.execute(text(mig._V_CRICOS_COVERAGE_AU_SQL))
        await db.commit()
    await engine.dispose()
    print("Migration 018 applied: v_cricos_coverage_au created")


if __name__ == "__main__":
    asyncio.run(_apply())
