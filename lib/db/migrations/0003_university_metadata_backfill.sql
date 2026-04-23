-- 0003_university_metadata_backfill.sql
--
-- Bug #4 (2026-04-23): production had country='Unknown' / city='Unknown'
-- on every existing university because the only validator was zod min(1).
-- That broke location-based search on the public Course Search page.
--
-- The route now rejects "Unknown" values for new universities. This
-- migration backfills the 6 known production rows so existing data is
-- correct too.
--
-- Idempotent: each UPDATE is gated on the current value being NULL or
-- 'Unknown' so re-running this migration won't clobber values the user
-- has since edited by hand.

BEGIN;

UPDATE universities
SET city = 'Toowoomba', country = 'Australia'
WHERE id = 1
  AND (city IS NULL OR LOWER(city) = 'unknown' OR country IS NULL OR LOWER(country) = 'unknown');

UPDATE universities
SET city = 'Sydney', country = 'Australia'
WHERE id = 2
  AND (city IS NULL OR LOWER(city) = 'unknown' OR country IS NULL OR LOWER(country) = 'unknown');

UPDATE universities
SET city = 'Sydney', country = 'Australia'
WHERE id = 3
  AND (city IS NULL OR LOWER(city) = 'unknown' OR country IS NULL OR LOWER(country) = 'unknown');

UPDATE universities
SET city = 'Bathurst', country = 'Australia'
WHERE id = 4
  AND (city IS NULL OR LOWER(city) = 'unknown' OR country IS NULL OR LOWER(country) = 'unknown');

UPDATE universities
SET city = 'Hobart', country = 'Australia'
WHERE id = 5
  AND (city IS NULL OR LOWER(city) = 'unknown' OR country IS NULL OR LOWER(country) = 'unknown');

UPDATE universities
SET city = 'Melbourne', country = 'Australia'
WHERE id = 6
  AND (city IS NULL OR LOWER(city) = 'unknown' OR country IS NULL OR LOWER(country) = 'unknown');

-- Refresh the public search MV so location filters reflect the new data.
-- Wrapped in DO so the migration still succeeds on environments where the
-- MV doesn't exist yet (fresh installs).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_matviews WHERE matviewname = 'course_search_view'
  ) THEN
    REFRESH MATERIALIZED VIEW CONCURRENTLY course_search_view;
  END IF;
END $$;

COMMIT;
