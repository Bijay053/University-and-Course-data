-- 0001_clean_course_names_and_locations.sql
--
-- Backfill cleanup for production bugs investigated 2026-04-23:
--
--   * 148 CSU courses stored as "Bachelor Business Studies" with the
--     preposition silently dropped (real titles say "Bachelor of …")
--   * 57 courses with garbage in `course_location` ("test", "On Campus",
--     Google error blurbs, foreign exchange-partner uni names, etc.)
--   * Duplicate rows in `scraped_field_evidence` from non-idempotent inserts
--
-- Idempotent: safe to re-run after a fresh DB import.

BEGIN;

-- ---------------------------------------------------------------------------
-- Restore "of" in degree names where the scraper stripped it.
-- ---------------------------------------------------------------------------

UPDATE courses
SET name = regexp_replace(name, '^(Bachelor|Master|Diploma|Doctor) ([A-Z])', '\1 of \2')
WHERE name NOT ILIKE '% of %'
  AND name NOT ILIKE '% in %'
  AND name ~ '^(Bachelor|Master|Diploma|Doctor) [A-Z]'
  AND name NOT ILIKE 'Bachelor Honours%'
  AND name NOT ILIKE 'Master Honours%';

UPDATE courses
SET name = regexp_replace(name, '^Graduate Certificate ([A-Z])', 'Graduate Certificate of \1')
WHERE name NOT ILIKE '% of %' AND name NOT ILIKE '% in %'
  AND name ~ '^Graduate Certificate [A-Z]';

UPDATE courses
SET name = regexp_replace(name, '^Graduate Diploma ([A-Z])', 'Graduate Diploma of \1')
WHERE name NOT ILIKE '% of %' AND name NOT ILIKE '% in %'
  AND name ~ '^Graduate Diploma [A-Z]';

UPDATE courses
SET name = regexp_replace(name, '^Undergraduate Certificate ([A-Z])', 'Undergraduate Certificate of \1')
WHERE name NOT ILIKE '% of %' AND name NOT ILIKE '% in %'
  AND name ~ '^Undergraduate Certificate [A-Z]';

-- ---------------------------------------------------------------------------
-- Null out garbage `course_location` values.
-- ---------------------------------------------------------------------------

UPDATE courses SET course_location = NULL
WHERE course_location ILIKE '%Session %'
   OR course_location ILIKE '%Google%'
   OR course_location ILIKE '%click here%'
   OR course_location = 'test'
   OR course_location ILIKE '%feedback%'
   OR course_location ILIKE '%trouble accessing%'
   OR course_location ILIKE '%Jilin%'
   OR course_location ILIKE '%Tianjin%'
   OR course_location ILIKE '%Yangzhou%'
   OR course_location ILIKE '%Yunnan%'
   OR course_location ILIKE '%SPACE University%'
   OR course_location ~* '^on campus$'
   OR LENGTH(course_location) > 150;

-- ---------------------------------------------------------------------------
-- De-duplicate scraped_field_evidence rows that were inserted multiple times.
-- ---------------------------------------------------------------------------

DELETE FROM scraped_field_evidence a
WHERE a.id > (
  SELECT MIN(b.id) FROM scraped_field_evidence b
  WHERE b.scraped_course_id = a.scraped_course_id
    AND b.field_key        = a.field_key
    AND b.candidate_value IS NOT DISTINCT FROM a.candidate_value
    AND b.source_url      IS NOT DISTINCT FROM a.source_url
);

-- Add a uniqueness guard so future inserts can use ON CONFLICT DO NOTHING.
-- (Partial unique index because candidate_value / source_url are nullable
-- and we want NULLs treated as equal for de-dup purposes.)
CREATE UNIQUE INDEX IF NOT EXISTS scraped_field_evidence_dedup
  ON scraped_field_evidence (
    scraped_course_id,
    field_key,
    COALESCE(candidate_value, ''),
    COALESCE(source_url, '')
  );

-- ---------------------------------------------------------------------------
-- Refresh the public search materialised view if present.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_matviews WHERE matviewname = 'course_search_view'
  ) THEN
    REFRESH MATERIALIZED VIEW CONCURRENTLY course_search_view;
  END IF;
EXCEPTION WHEN OTHERS THEN
  -- Concurrent refresh requires a unique index; fall back to plain refresh.
  IF EXISTS (
    SELECT 1 FROM pg_matviews WHERE matviewname = 'course_search_view'
  ) THEN
    REFRESH MATERIALIZED VIEW course_search_view;
  END IF;
END $$;

COMMIT;
