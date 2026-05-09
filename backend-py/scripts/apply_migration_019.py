"""Apply migration 019 (users + permissions + reset tokens) directly via asyncpg.

Per replit.md, alembic does not work on prod. Idempotent -- safe to re-run.

    cd backend-py && PYTHONPATH=. python scripts/apply_migration_019.py
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
        / "alembic/versions/019_user_permissions.py"
    )
    spec = importlib.util.spec_from_file_location("mig019", mig_path)
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)  # type: ignore[union-attr]

    blocks = [mig._USERS_SQL, mig._USER_PERMISSIONS_SQL, mig._RESET_TOKENS_SQL]
    statements = [
        s.strip()
        for block in blocks
        for s in block.split(";")
        if s.strip()
    ]
    async with AsyncSessionLocal() as db:
        for stmt in statements:
            await db.execute(text(stmt))
        await db.commit()
    await engine.dispose()
    print("Migration 019 applied: users, user_permissions, password_reset_tokens")


if __name__ == "__main__":
    asyncio.run(_apply())
