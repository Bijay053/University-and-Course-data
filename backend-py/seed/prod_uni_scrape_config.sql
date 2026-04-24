-- Backfill scrape_config.uniPages on production from dev data.
--
-- Generated: 2026-04-24 from dev DB (heliumdb).
-- Mapping: prod id ←→ dev id (matched by domain, NOT by id — IDs do not align).
--
-- Prod has 6 unis; only 3 have matching scrape_config payloads in dev:
--   prod 1 (USQ)        → no dev match — SKIPPED
--   prod 2 (asa)        → dev id=9   → UPDATE below
--   prod 3 (torrens)    → dev id=5   → UPDATE below
--   prod 4 (CSU)        → no dev match — SKIPPED
--   prod 5 (UTAS)       → no dev match — SKIPPED
--   prod 6 (vit)        → dev id=16  → UPDATE below
--
-- Safe to re-run (idempotent: jsonb_set with create_missing=true overwrites uniPages
-- in place without touching courseLinks / resolvedUrl / lastScrapedAt or other keys).
--
-- Run on prod with:
--   psql "$DATABASE_URL" -f backend-py/seed/prod_uni_scrape_config.sql
--
-- Verify after:
--   SELECT id, name, scrape_config->'uniPages' FROM universities WHERE id IN (2,3,6);

BEGIN;

-- ── prod 2: asa  (← dev id=9, http://asahe.edu.au/) ────────────────────────────
UPDATE universities
SET scrape_config = jsonb_set(
  COALESCE(scrape_config, '{}'::jsonb),
  '{uniPages}',
  '{
    "feePage": "http://asahe.edu.au/fees-and-charges",
    "feesPdf": "https://cdn.prod.website-files.com/68660d9286e56f070b7bebe7/696f704ddf123bd8c8d982b0_2026%20Fees%20Schedule%20-%20International%20Student.pdf",
    "requirementsPage": "http://asahe.edu.au/policies-and-forms",
    "requirementsPdf": "https://cdn.prod.website-files.com/68660d9386e56f070b7bec59/69af540baceac672ce4d06db_Student%20Admissions%20Policy%202025.2.pdf"
  }'::jsonb,
  true
),
updated_at = now()
WHERE id = 2;

-- ── prod 3: torrens  (← dev id=5, https://www.torrens.edu.au/) ────────────────
UPDATE universities
SET scrape_config = jsonb_set(
  COALESCE(scrape_config, '{}'::jsonb),
  '{uniPages}',
  '{
    "feePage": "https://www.torrens.edu.au/international-fees",
    "feesPdf": "https://cdn.intelligencebank.com/au/share/RyzZ/D1G8V/jlMdo/original/2026-International-Fee-Schedule",
    "entryPage": "https://www.torrens.edu.au/courses/english",
    "requirementsPage": "https://www.torrens.edu.au/policies-and-forms"
  }'::jsonb,
  true
),
updated_at = now()
WHERE id = 3;

-- ── prod 6: vit  (← dev id=16, https://vit.edu.au/) ───────────────────────────
UPDATE universities
SET scrape_config = jsonb_set(
  COALESCE(scrape_config, '{}'::jsonb),
  '{uniPages}',
  '{
    "feePage": "https://vit.edu.au/international/fees",
    "requirementsPage": "https://vit.edu.au/resources/course-entry-requirements"
  }'::jsonb,
  true
),
updated_at = now()
WHERE id = 6;

-- ── Sanity check before commit ────────────────────────────────────────────────
-- Expect 3 rows, each with a non-null uniPages JSON object.
SELECT id, name, scrape_config->'uniPages' AS uni_pages
FROM universities
WHERE id IN (2, 3, 6)
ORDER BY id;

COMMIT;

-- ── Not backfilled (no dev source) ────────────────────────────────────────────
-- prod 1 (USQ),  prod 4 (CSU),  prod 5 (UTAS):
-- Run a fresh scrape against their listing URLs to populate scrape_config, e.g.
--   POST /api/scrape/start  {"universityId": 1, "urls": ["https://www.unisq.edu.au/"]}
