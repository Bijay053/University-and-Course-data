-- ============================================================
-- Staging-row repair for UK universities — run directly with psql
-- No Python dependency; safe to run multiple times (idempotent).
--
-- Usage on production:
--   sudo -u postgres psql -d university_portal \
--     -f /root/University-and-Course-data/backend-py/scripts/sql_repair_staging.sql
-- ============================================================

BEGIN;

-- ── 1. course_location: strip "University: " prefix ──────────
-- "University: City Campus"      → "City Campus"
-- "University: City Campus, University: Springfield Campus"
--                                → "City Campus, Springfield Campus"
-- "University:" (bare)           → NULL
UPDATE scraped_courses
SET course_location = NULLIF(
    TRIM(REGEXP_REPLACE(course_location, 'University\s*:\s*', '', 'gi')),
    ''
)
WHERE course_location ILIKE '%University:%'
  AND status NOT IN ('rejected', 'approved');

-- bare delivery-mode labels → NULL
UPDATE scraped_courses
SET course_location = NULL
WHERE LOWER(TRIM(course_location)) IN (
    'mode', 'delivery method', 'delivery', 'online', 'virtual',
    'on campus', 'blended'
)
  AND status NOT IN ('rejected', 'approved');

-- ── 2. degree_level: infer from course_name where NULL ───────

-- Canonicalise bare "Master" / "Bachelor" (old AI output without 's)
UPDATE scraped_courses
SET degree_level = 'Master''s'
WHERE LOWER(TRIM(degree_level)) = 'master'
  AND status NOT IN ('rejected', 'approved');

UPDATE scraped_courses
SET degree_level = 'Bachelor''s'
WHERE LOWER(TRIM(degree_level)) = 'bachelor'
  AND status NOT IN ('rejected', 'approved');

-- Master's: MA, MPH, LLM, MSc, MBA, MEd, MPhil, MRes
UPDATE scraped_courses
SET degree_level = 'Master''s'
WHERE (degree_level IS NULL OR degree_level = '')
  AND (
       course_name ~* '^\s*MA\s+'        OR course_name ~* '^\s*M\.A\.\s'
    OR course_name ~* '^\s*MPH[\s\(]'
    OR course_name ~* '^\s*LLM[\s\(]'
    OR course_name ~* '^\s*MSc[\s\(]'   OR course_name ~* '^\s*Msc[\s\(]'
    OR course_name ~* '^\s*MBA[\s\(]'
    OR course_name ~* '^\s*MEd[\s\(]'
    OR course_name ~* '^\s*MPhil'
    OR course_name ~* '^\s*MRes[\s\(]'
    OR course_name ~* '^\s*Master'
  )
  AND status NOT IN ('rejected', 'approved');

-- Bachelor's: LLB, BNurs, BMid, MPharm, HNC, HND, Fd (Arts/Sci), FdA, FdSc
UPDATE scraped_courses
SET degree_level = 'Bachelor''s'
WHERE (degree_level IS NULL OR degree_level = '')
  AND (
       course_name ~* '^\s*LLB'
    OR course_name ~* '^\s*BNurs'
    OR course_name ~* '^\s*BMid'
    OR course_name ~* '^\s*MPharm'
    OR course_name ~* '^\s*HNC[\s\(]'
    OR course_name ~* '^\s*HND[\s\(]'
    OR course_name ~* '^\s*Fd\s*\(Arts'
    OR course_name ~* '^\s*Fd\s*\(Sci'
    OR course_name ~* '^\s*FdA[\s\(]'
    OR course_name ~* '^\s*FdSc[\s\(]'
    OR course_name ~* '^\s*Foundation Degree'
  )
  AND status NOT IN ('rejected', 'approved');

-- ── 3. Domestic fee clearing: GBP < £10,000 ─────────────────
-- Home / module / CPD / part-time fees — never a valid international fee.
UPDATE scraped_courses
SET international_fee = NULL,
    fee_term          = NULL,
    currency          = NULL
WHERE currency = 'GBP'
  AND international_fee < 10000
  AND status NOT IN ('rejected', 'approved');

-- ── 4. Wrong-currency fees on UK universities ─────────────────
-- All non-GBP fees on .ac.uk / .co.uk universities are misdetections:
--   AUD — old code defaulted to AUD when no explicit currency symbol
--   CAD — r"C\$" regex matched "Course$18,645" or "C $18,645" on WLV pages
-- Clear them so re-scrape with new Pre-pass 0 fee table extractor picks
-- up the correct GBP International+Full-time row.
UPDATE scraped_courses sc
SET international_fee = NULL,
    fee_term          = NULL,
    currency          = NULL
FROM universities u
WHERE sc.university_id = u.id
  AND sc.currency IS NOT NULL
  AND sc.currency != 'GBP'
  AND sc.international_fee IS NOT NULL
  AND sc.status NOT IN ('rejected', 'approved')
  AND (
       u.scrape_url ILIKE '%.ac.uk%'
    OR u.scrape_url ILIKE '%.co.uk%'
    OR u.scrape_url ILIKE '%://%.uk/%'
  );

-- ── Summary (zero counts = clean) ────────────────────────────
SELECT
  'Remaining University: prefix in locations' AS check_name,
  COUNT(*) AS count
FROM scraped_courses
WHERE course_location ILIKE '%University:%'
  AND status NOT IN ('rejected', 'approved')
UNION ALL
SELECT
  'Remaining delivery-mode-only locations',
  COUNT(*)
FROM scraped_courses
WHERE LOWER(TRIM(course_location)) IN ('mode','delivery method','online')
  AND status NOT IN ('rejected', 'approved')
UNION ALL
SELECT
  'Remaining NULL degree_level for MA/LLB/HNC names',
  COUNT(*)
FROM scraped_courses
WHERE (degree_level IS NULL OR degree_level = '')
  AND course_name ~* '^\s*(MA[\s\(]|MPH[\s\(]|LLM[\s\(]|MSc[\s\(]|MBA[\s\(]|LLB|BNurs|BMid|MPharm|HNC[\s\(]|HND[\s\(]|Fd\s*\()'
  AND status NOT IN ('rejected', 'approved')
UNION ALL
SELECT
  'Remaining GBP domestic fees (< £10k)',
  COUNT(*)
FROM scraped_courses
WHERE currency = 'GBP' AND international_fee < 10000
  AND status NOT IN ('rejected', 'approved')
UNION ALL
SELECT
  'Remaining non-GBP fees on UK universities',
  COUNT(*)
FROM scraped_courses sc
JOIN universities u ON u.id = sc.university_id
WHERE sc.currency IS NOT NULL AND sc.currency != 'GBP'
  AND sc.international_fee IS NOT NULL
  AND sc.status NOT IN ('rejected', 'approved')
  AND (u.scrape_url ILIKE '%.ac.uk%' OR u.scrape_url ILIKE '%.co.uk%');

COMMIT;
