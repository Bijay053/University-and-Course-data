"""Migration 022 — Prune scraper_config_history to _HISTORY_KEEP (100) rows per slug.

The history table was created in migration 021.  Before the prune logic was
wired into the restore endpoint it was possible for rows to accumulate beyond
the 100-row cap.  This one-off migration deletes any rows already over the
limit so the table is bounded from this point forward.

Apply on prod:
  cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_022.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine

_HISTORY_KEEP = 100


async def main() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT COUNT(DISTINCT slug) FROM scraper_config_history
        """))
        slug_count = result.scalar() or 0

        result = await conn.execute(text("""
            SELECT COUNT(*) FROM scraper_config_history
        """))
        total_before = result.scalar() or 0

        await conn.execute(text("""
            DELETE FROM scraper_config_history
            WHERE id NOT IN (
                SELECT id
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY slug
                               ORDER BY saved_at DESC
                           ) AS rn
                    FROM scraper_config_history
                ) ranked
                WHERE rn <= :keep
            )
        """), {"keep": _HISTORY_KEEP})

        result = await conn.execute(text("""
            SELECT COUNT(*) FROM scraper_config_history
        """))
        total_after = result.scalar() or 0

    deleted = total_before - total_after
    print(
        f"Migration 022 applied successfully. "
        f"Slugs processed: {slug_count}. "
        f"Rows before: {total_before}, after: {total_after}, deleted: {deleted}."
    )


if __name__ == "__main__":
    asyncio.run(main())
