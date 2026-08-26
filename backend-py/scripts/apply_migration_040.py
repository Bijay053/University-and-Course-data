#!/usr/bin/env python3
"""Migration 040 — course_sub_categories table + canonical seed data.

Creates:
  - course_sub_categories: persistent, auto-growing sub-category vocabulary.
    Each row is a (category, sub_category) pair valid for course classification.
    The table is pre-seeded from the frontend course-constants.ts canonical list.
    New Gemini-generated sub-categories are added automatically at approve time
    (auto_added=True) via app.services.sub_category_matcher.resolve_sub_category().

Why this migration exists:
  The table is used by stage_course.py to canonicalise free-text sub_category
  values extracted by Gemini. If the table is absent, every staging attempt
  raises "relation course_sub_categories does not exist" and the course is NOT
  staged (data loss). Previously, the table was created by a Drizzle schema push
  on Replit but was not tracked as a numbered migration — meaning fresh local
  clones would hit the error.

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_040.py

Apply on prod:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_040.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal
from app.services.scraper.taxonomy import LEGACY_PARENT_ALIASES, TAXONOMY_PAIRS

_SEED = TAXONOMY_PAIRS

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS course_sub_categories (
    id          SERIAL PRIMARY KEY,
    category    TEXT        NOT NULL,
    sub_category TEXT       NOT NULL,
    auto_added  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_course_sub_cat UNIQUE (category, sub_category)
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_course_sub_cat_category
    ON course_sub_categories (category)
"""


async def run() -> None:
    async with AsyncSessionLocal() as db:
        async with db.begin():
            # 1. Create table
            await db.execute(text(_CREATE_TABLE))
            await db.execute(text(_CREATE_INDEX))
            print("Table and index created (or already existed).")

            # 2. Move legacy vocabulary rows to current parents. This only
            #    changes the controlled vocabulary table; manually classified
            #    course rows are deliberately left untouched.
            for legacy, canonical in LEGACY_PARENT_ALIASES.items():
                await db.execute(
                    text("""
                        INSERT INTO course_sub_categories
                            (category, sub_category, auto_added, created_at)
                        SELECT :canonical, sub_category, auto_added, created_at
                        FROM course_sub_categories
                        WHERE category = :legacy
                        ON CONFLICT (category, sub_category) DO NOTHING
                    """),
                    {"legacy": legacy, "canonical": canonical},
                )
                await db.execute(
                    text(
                        "DELETE FROM course_sub_categories WHERE category = :legacy"
                    ),
                    {"legacy": legacy},
                )

            # 3. Seed canonical rows — INSERT … ON CONFLICT DO NOTHING
            #    so re-runs are safe and auto_added=True rows are never touched.
            inserted = 0
            for category, sub_category in _SEED:
                result = await db.execute(
                    text("""
                        INSERT INTO course_sub_categories (category, sub_category, auto_added)
                        VALUES (:cat, :sub, FALSE)
                        ON CONFLICT (category, sub_category) DO NOTHING
                    """),
                    {"cat": category, "sub": sub_category},
                )
                inserted += result.rowcount

            total = len(_SEED)
            skipped = total - inserted
            print(
                f"Seed: inserted {inserted}/{total} canonical rows "
                f"({skipped} already existed — skipped)."
            )

        # Verify
        async with db.begin():
            count = (
                await db.execute(text("SELECT COUNT(*) FROM course_sub_categories"))
            ).scalar()
            print(f"Total rows in course_sub_categories: {count}")


if __name__ == "__main__":
    asyncio.run(run())
