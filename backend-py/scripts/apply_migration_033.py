"""Migration 033 — Phase 14: Autonomous Publishing & Review Engine.

Adds four columns to scraped_courses and creates the publishing_ledger table.

Apply on production:
    sudo -u postgres psql -d university_portal -f - << 'SQL'
    ALTER TABLE scraped_courses
      ADD COLUMN IF NOT EXISTS pub_score FLOAT,
      ADD COLUMN IF NOT EXISTS pub_score_breakdown JSONB,
      ADD COLUMN IF NOT EXISTS pub_decision TEXT,
      ADD COLUMN IF NOT EXISTS pub_decision_reason TEXT;

    CREATE TABLE IF NOT EXISTS publishing_ledger (
        id SERIAL PRIMARY KEY,
        scraped_course_id INTEGER REFERENCES scraped_courses(id) ON DELETE SET NULL,
        university_id INTEGER NOT NULL REFERENCES universities(id) ON DELETE CASCADE,
        course_name TEXT NOT NULL,
        action TEXT NOT NULL,
        pub_score FLOAT,
        pub_score_breakdown JSONB,
        actor TEXT NOT NULL DEFAULT 'system',
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_publishing_ledger_uni    ON publishing_ledger(university_id);
    CREATE INDEX IF NOT EXISTS idx_publishing_ledger_action ON publishing_ledger(action);
    CREATE INDEX IF NOT EXISTS idx_publishing_ledger_created ON publishing_ledger(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sc_pub_decision          ON scraped_courses(pub_decision);
    SQL

Verify:
    sudo -u postgres psql -d university_portal -c "
    SELECT column_name FROM information_schema.columns
    WHERE table_name='scraped_courses' AND column_name LIKE 'pub_%'
    ORDER BY column_name;"

    sudo -u postgres psql -d university_portal -c "\\d publishing_ledger"
"""
import asyncio
from sqlalchemy import text
from app.legacy_migration_database import create_migration_engine

SQL = """
ALTER TABLE scraped_courses
  ADD COLUMN IF NOT EXISTS pub_score FLOAT,
  ADD COLUMN IF NOT EXISTS pub_score_breakdown JSONB,
  ADD COLUMN IF NOT EXISTS pub_decision TEXT,
  ADD COLUMN IF NOT EXISTS pub_decision_reason TEXT;

CREATE TABLE IF NOT EXISTS publishing_ledger (
    id SERIAL PRIMARY KEY,
    scraped_course_id INTEGER REFERENCES scraped_courses(id) ON DELETE SET NULL,
    university_id INTEGER NOT NULL REFERENCES universities(id) ON DELETE CASCADE,
    course_name TEXT NOT NULL,
    action TEXT NOT NULL,
    pub_score FLOAT,
    pub_score_breakdown JSONB,
    actor TEXT NOT NULL DEFAULT 'system',
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_publishing_ledger_uni    ON publishing_ledger(university_id);
CREATE INDEX IF NOT EXISTS idx_publishing_ledger_action ON publishing_ledger(action);
CREATE INDEX IF NOT EXISTS idx_publishing_ledger_created ON publishing_ledger(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sc_pub_decision          ON scraped_courses(pub_decision);
"""

async def main():
    engine = create_migration_engine(echo=False)
    try:
        async with engine.begin() as connection:
            # Fixed simple DDL only; preserve psql -c's single transaction.
            for statement in SQL.split(";"):
                if statement.strip():
                    await connection.execute(text(statement.strip()))
    finally:
        await engine.dispose()
    print("Migration 033 complete.")


if __name__ == "__main__":
    asyncio.run(main())
