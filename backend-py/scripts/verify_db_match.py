"""Compare SQLAlchemy model metadata to the live Postgres schema.

Exits 0 if every model table exists with the expected columns (extra
columns in the DB are tolerated -- it's a one-way "models <= db" check
because Drizzle owns the schema). Exits 1 with a diff otherwise.

Usage:
    cd backend-py
    python scripts/verify_db_match.py
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app import models  # noqa: F401  -- registers all tables on Base.metadata
from app.database import Base, engine


async def _live_columns() -> dict[str, set[str]]:
    sql = text(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        """
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(sql)).all()
    out: dict[str, set[str]] = {}
    for tbl, col in rows:
        out.setdefault(tbl, set()).add(col)
    return out


async def main() -> int:
    live = await _live_columns()
    missing_tables: list[str] = []
    missing_cols: list[tuple[str, str]] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in live:
            missing_tables.append(table_name)
            continue
        for col in table.columns:
            if col.name not in live[table_name]:
                missing_cols.append((table_name, col.name))

    if not missing_tables and not missing_cols:
        print(f"OK — all {len(Base.metadata.tables)} model tables match the live schema.")
        return 0

    print("MISMATCH:")
    if missing_tables:
        print("  Missing tables:")
        for t in sorted(missing_tables):
            print(f"    - {t}")
    if missing_cols:
        print("  Missing columns:")
        for t, c in sorted(missing_cols):
            print(f"    - {t}.{c}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
