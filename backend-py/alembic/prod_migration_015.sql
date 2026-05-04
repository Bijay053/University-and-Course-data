-- Production migration 015: add ALL columns that exist in the ORM models but
-- were never captured in an Alembic migration.
--
-- Root cause of HTTP 500 on GET /api/courses:
--   The courses table on prod was bootstrapped from an older create_all
--   snapshot.  SQLAlchemy's SELECT courses.* enumerates every column
--   declared in the model; missing columns cause PostgreSQL to raise
--   UndefinedColumn → FastAPI returns 500.
--
-- This script is fully idempotent (ADD COLUMN IF NOT EXISTS everywhere).
-- Safe to run multiple times, regardless of which migrations are already
-- applied on prod.
--
-- ── How to run ─────────────────────────────────────────────────────────────
--
--   sudo -u postgres psql university_portal \
--       -f /root/University-and-Course-data/backend-py/alembic/prod_migration_015.sql
--
-- Then restart the API:
--
--   pm2 restart uni-api-py
--   # or: systemctl restart uni-api-py
--
-- Verify:
--   curl -s "http://localhost/api/courses?universityId=2&limit=1"
--   # Must return {"data":[...],"total":N,...} — NOT a 500.
-- ───────────────────────────────────────────────────────────────────────────

-- ── 1. courses table — add all columns missing from the original schema ────

ALTER TABLE courses ADD COLUMN IF NOT EXISTS sub_category           TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS course_structure       TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS career_outcomes        TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS course_location        TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS student_market         TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS delivery_mode          TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS international_eligible BOOLEAN;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS on_campus_available    BOOLEAN;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS eligibility_reason     TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS eligibility_confidence FLOAT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS approval_score         FLOAT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS approved_at            TIMESTAMPTZ;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_reviewed_at       TIMESTAMPTZ;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_edited_at         TIMESTAMPTZ;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_edited_by         TEXT;

-- NOT NULL columns need a DEFAULT so existing rows satisfy the constraint.
-- PostgreSQL ADD COLUMN IF NOT EXISTS is a no-op when the column exists,
-- so these are safe to run even if the columns are already present.
ALTER TABLE courses ADD COLUMN IF NOT EXISTS eligibility_status
    TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE courses ADD COLUMN IF NOT EXISTS approval_status
    TEXT NOT NULL DEFAULT 'approved';

-- ── 2. universities table — add columns added after initial deployment ──────

ALTER TABLE universities ADD COLUMN IF NOT EXISTS scrape_config               JSONB;
ALTER TABLE universities ADD COLUMN IF NOT EXISTS fee_page_url                TEXT;
ALTER TABLE universities ADD COLUMN IF NOT EXISTS requirements_page_url       TEXT;
ALTER TABLE universities ADD COLUMN IF NOT EXISTS scholarship_page_url        TEXT;
ALTER TABLE universities ADD COLUMN IF NOT EXISTS academic_requirements_page_url TEXT;
ALTER TABLE universities ADD COLUMN IF NOT EXISTS featured
    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE universities ADD COLUMN IF NOT EXISTS featured_priority
    INTEGER NOT NULL DEFAULT 0;

-- ── 3. Refresh materialized view (if it exists) ───────────────────────────
-- Uses plain REFRESH (not CONCURRENTLY) so it works even without a unique index.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_matviews WHERE matviewname = 'course_search_view') THEN
        REFRESH MATERIALIZED VIEW course_search_view;
        RAISE NOTICE 'course_search_view refreshed OK';
    ELSE
        RAISE NOTICE 'course_search_view does not exist yet — skipping refresh';
    END IF;
END $$;

-- ── 4. Mark migration applied ───────────────────────────────────────────────
-- Check current state first:
--   SELECT version_num FROM alembic_version;
--
-- Migration 015 has no dependency on 012-014 for the ALTER TABLE statements
-- and is safe to apply at any point in the migration chain.
--
-- Advance the version pointer to 015 (idempotent):
INSERT INTO alembic_version (version_num)
VALUES ('015_courses_missing_columns')
ON CONFLICT DO NOTHING;

-- If the version is currently an OLDER revision, replace it:
UPDATE alembic_version
SET version_num = '015_courses_missing_columns'
WHERE version_num != '015_courses_missing_columns';

\echo ''
\echo '================================================================'
\echo 'Migration 015 applied OK.'
\echo 'courses and universities tables now have all required columns.'
\echo 'Restart the API (pm2 restart uni-api-py) then verify:'
\echo '  curl -s http://localhost/api/courses?universityId=2\&limit=1'
\echo '================================================================'
