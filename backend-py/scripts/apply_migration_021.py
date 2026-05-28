"""Migration 021 — Scraper config edit history.

Creates:
  - scraper_config_history (id, slug, yaml_content, saved_by, saved_at)

Apply on prod:
  cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_021.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine


async def main() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scraper_config_history (
                id           SERIAL PRIMARY KEY,
                slug         VARCHAR(64) NOT NULL,
                yaml_content TEXT        NOT NULL,
                saved_by     VARCHAR(255),
                saved_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_scraper_config_history_slug
                ON scraper_config_history (slug, saved_at DESC);
        """))
    print("Migration 021 applied successfully.")


if __name__ == "__main__":
    asyncio.run(main())
