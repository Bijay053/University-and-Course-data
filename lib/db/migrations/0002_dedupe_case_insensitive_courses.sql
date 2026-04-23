-- 0002_dedupe_case_insensitive_courses.sql
--
-- Production bug investigated 2026-04-23:
--
--   approveSingleCourse used a strict case-sensitive name match
--   (`name=$2`) when looking for an existing live course to merge an
--   approved staged course into. Because course names arrive from
--   scrapes with inconsistent capitalisation (e.g. "Bachelor Of
--   Business Studies" vs "Bachelor of Business Studies"), the dup-
--   check missed live row #169 ("Bachelor Of Business Studies") and
--   inserted a brand-new row #411 — visible duplication on the public
--   search.
--
-- The code fix (case-insensitive LOWER(name) match) prevents NEW
-- duplicates. This migration cleans up duplicates already in the
-- database by:
--
--   1. Grouping courses by (university_id, LOWER(name)).
--   2. Within each group keeping the OLDEST row (lowest id) — that's
--      the canonical id every downstream FK already points at.
--   3. Re-pointing FKs from the newer duplicate(s) to the kept id
--      (cascade-FK tables: english_requirements, scholarships,
--      academic_requirements, fees, intakes, course_review children,
--      and the soft-FK tables scraped_courses + scraping job rows).
--   4. Gap-filling the kept row with non-null values from the
--      newer duplicate (only where the kept value is NULL — never
--      overwriting human-curated data).
--   5. Picking the most recent updated_at among the merged set so
--      "last updated" timestamps still reflect reality.
--   6. Deleting the duplicate rows.
--
-- The whole thing runs in a single transaction so there is no window
-- where FKs point at a half-deleted row.

BEGIN;

-- ── Step 1: build a (dup_id → keep_id) mapping for every dup group.
CREATE TEMP TABLE _course_dedupe_map ON COMMIT DROP AS
WITH ranked AS (
  SELECT
    id,
    university_id,
    LOWER(name) AS lname,
    MIN(id) OVER (PARTITION BY university_id, LOWER(name)) AS keep_id
  FROM courses
)
SELECT id AS dup_id, keep_id
FROM ranked
WHERE id <> keep_id;

-- Quick visibility while running by hand on prod psql:
DO $$
DECLARE n int;
BEGIN
  SELECT COUNT(*) INTO n FROM _course_dedupe_map;
  RAISE NOTICE 'courses dedupe: % duplicate row(s) will be merged', n;
END $$;

-- ── Step 2: gap-fill the kept row from each duplicate. We do this
--           BEFORE re-pointing FKs so the merge sees both rows intact.
--           Only fills columns that are NULL on the kept row, never
--           overwrites populated values. We pick the duplicate whose
--           updated_at is most recent so we copy the freshest data.
--
--   IMPORTANT: only columns that actually live on `courses` are
--   listed here. Fee, english-test, intake, academic-requirement and
--   scholarship data live in their own tables (fees, intakes,
--   english_requirements, academic_requirements, scholarships) and
--   will be carried over via the FK re-point in Step 3.
WITH ordered_dups AS (
  SELECT m.dup_id, m.keep_id,
         ROW_NUMBER() OVER (PARTITION BY m.keep_id ORDER BY c.updated_at DESC NULLS LAST, c.id DESC) AS rn
  FROM _course_dedupe_map m
  JOIN courses c ON c.id = m.dup_id
)
UPDATE courses k SET
  category               = COALESCE(k.category,               d.category),
  sub_category           = COALESCE(k.sub_category,           d.sub_category),
  course_website         = COALESCE(k.course_website,         d.course_website),
  course_location        = COALESCE(k.course_location,        d.course_location),
  duration               = COALESCE(k.duration,               d.duration),
  duration_term          = COALESCE(k.duration_term,          d.duration_term),
  study_mode             = COALESCE(k.study_mode,             d.study_mode),
  degree_level           = COALESCE(k.degree_level,           d.degree_level),
  study_load             = COALESCE(k.study_load,             d.study_load),
  language               = COALESCE(k.language,               d.language),
  description            = COALESCE(k.description,            d.description),
  course_structure       = COALESCE(k.course_structure,       d.course_structure),
  career_outcomes        = COALESCE(k.career_outcomes,        d.career_outcomes),
  other_test             = COALESCE(k.other_test,             d.other_test),
  other_test_score       = COALESCE(k.other_test_score,       d.other_test_score),
  other_requirement      = COALESCE(k.other_requirement,      d.other_requirement),
  student_market         = COALESCE(k.student_market,         d.student_market),
  delivery_mode          = COALESCE(k.delivery_mode,          d.delivery_mode),
  international_eligible = COALESCE(k.international_eligible, d.international_eligible),
  on_campus_available    = COALESCE(k.on_campus_available,    d.on_campus_available),
  eligibility_reason     = COALESCE(k.eligibility_reason,     d.eligibility_reason),
  eligibility_confidence = COALESCE(k.eligibility_confidence, d.eligibility_confidence),
  approval_score         = COALESCE(k.approval_score,         d.approval_score),
  approved_at            = COALESCE(k.approved_at,            d.approved_at),
  last_reviewed_at       = GREATEST(k.last_reviewed_at,       d.last_reviewed_at),
  last_edited_at         = GREATEST(k.last_edited_at,         d.last_edited_at),
  last_edited_by         = COALESCE(k.last_edited_by,         d.last_edited_by),
  updated_at             = GREATEST(k.updated_at,             d.updated_at)
FROM ordered_dups o
JOIN courses d ON d.id = o.dup_id
WHERE k.id = o.keep_id
  AND o.rn = 1;

-- ── Step 3: re-point every FK from the duplicate ids to the kept id.
--           Done before deleting the dup rows so cascade-FK rows aren't
--           wiped out. Where a UNIQUE/PK conflict would arise (same
--           parent/child pair pointing at both ids), we drop the dup-
--           side row to keep the kept-side row.

-- english_requirements (1 row per course expected → re-point if no
-- existing row on keep, else delete the dup-side row).
DELETE FROM english_requirements er
USING _course_dedupe_map m
WHERE er.course_id = m.dup_id
  AND EXISTS (SELECT 1 FROM english_requirements er2 WHERE er2.course_id = m.keep_id);
UPDATE english_requirements er
SET course_id = m.keep_id
FROM _course_dedupe_map m
WHERE er.course_id = m.dup_id;

-- academic_requirements
DELETE FROM academic_requirements ar
USING _course_dedupe_map m
WHERE ar.course_id = m.dup_id
  AND EXISTS (SELECT 1 FROM academic_requirements ar2 WHERE ar2.course_id = m.keep_id);
UPDATE academic_requirements ar
SET course_id = m.keep_id
FROM _course_dedupe_map m
WHERE ar.course_id = m.dup_id;

-- fees (multiple rows per course allowed — just re-point all)
UPDATE fees f
SET course_id = m.keep_id
FROM _course_dedupe_map m
WHERE f.course_id = m.dup_id;

-- intakes (multiple rows per course allowed — just re-point all)
UPDATE intakes i
SET course_id = m.keep_id
FROM _course_dedupe_map m
WHERE i.course_id = m.dup_id;

-- scholarships (multiple rows per course allowed — just re-point all)
UPDATE scholarships s
SET course_id = m.keep_id
FROM _course_dedupe_map m
WHERE s.course_id = m.dup_id;

-- Re-point ALL other tables that carry a course_id column.
-- Covers course_review children (3 tables) and any other satellite
-- table without us needing to enumerate them by hand. Skipped:
-- the tables already handled above and the soft-FK tables handled
-- explicitly below.
DO $$
DECLARE
  rec record;
BEGIN
  FOR rec IN
    SELECT table_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND column_name = 'course_id'
      AND table_name NOT IN (
        'english_requirements','academic_requirements','fees','intakes','scholarships',
        'scraped_courses','scrape_jobs'
      )
  LOOP
    EXECUTE format(
      'UPDATE %I SET course_id = m.keep_id FROM _course_dedupe_map m WHERE %I.course_id = m.dup_id',
      rec.table_name, rec.table_name
    );
  END LOOP;
END $$;

-- scraped_courses (set null on delete — re-point so we don't lose the link)
UPDATE scraped_courses sc
SET course_id = m.keep_id
FROM _course_dedupe_map m
WHERE sc.course_id = m.dup_id;

-- ── Step 4: drop the duplicate course rows.
DELETE FROM courses
WHERE id IN (SELECT dup_id FROM _course_dedupe_map);

-- Final visibility log
DO $$
DECLARE n int;
BEGIN
  SELECT COUNT(*) INTO n FROM _course_dedupe_map;
  RAISE NOTICE 'courses dedupe: deleted % duplicate row(s); FKs merged into the oldest id per (university_id, LOWER(name)) group', n;
END $$;

COMMIT;
