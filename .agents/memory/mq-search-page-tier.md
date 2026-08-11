---
name: MQ discovery resolver fix
description: MQ fetch_failed root cause + fix: resolver now infers URL prefix from title (undergraduate/postgraduate/research) instead of always /courses/; search page uses networkidle.
---

# MQ Discovery Fix — Resolver Prefix Inference

## Root Cause of 113/367 staging
The coursehandbook resolver always constructed `/study/find-a-course/courses/<slug>`. Many MQ courses
actually live at `/undergraduate/<slug>`, `/postgraduate/<slug>`, or `/research/<slug>`. In the Aug 2026
production run, 79/198 (40%) courses got `fetch_failed` because the constructed URL 404s.

## Fix Applied
`_resolve_to_study_urls._resolve_one()` now uses two strategies instead of always `/courses/`:

1. **Primary**: Extract a direct admissions link embedded in the coursehandbook HTML  
   (`_ADMISSIONS_URL_IN_PAGE_RE` regex on the page body — handbook sometimes has "Apply" links)

2. **Secondary**: Infer path prefix from course title via `_infer_url_prefix()`:
   - "Bachelor of *" / "Diploma *" / "Associate Degree" → `undergraduate`
   - "Master of *" / "Graduate Certificate *" / "Graduate Diploma *" → `postgraduate`
   - "Doctor of *" / "PhD" / "Professional Doctorate" → `research`
   - Otherwise → `courses` (safe fallback)

`_STUDY_URL_ROOT = "https://www.mq.edu.au/study/find-a-course/"` is the new root constant.
`_STUDY_URL_BASE` (the `/courses/` variant) is retained as the fallback constant.

## Search Page (Tier 1.5)
Switched from `wait_until="domcontentloaded"` to `wait_until="networkidle"` (50s timeout) so
Funnelback XHR results are included before the DOM is read. Also changed the selector to be
course-level specific (requires `/undergraduate/` or `/postgraduate/` or `/research/` or `/courses/`
in the href) rather than just any `/study/find-a-course/` nav link (which fired too early).

## Year Filter
`_COURSEHANDBOOK_YEARS` contains 3 years (prev + curr + next). The test was updated from
`len == 2` to `len == 3`. Prior year is intentional: ~50-80 unique courses only appear in
the previous year's sitemap (not yet re-published for the current year).

## How to Apply
If MQ staging count drops below 300, check:
1. Job log for `[DISCOVER] MQ: resolver skipped N — reasons:` — `http_404×Y` means URLs
   still hitting the wrong prefix (possibly a new degree type not in `_TITLE_LEVEL_PREFIXES`)
2. Job log for `[DISCOVER] MQ: search page N diagnostic` — if `selector_found=False` and
   `total_anchors < 30`, CF challenge is blocking the search page
3. Add new patterns to `_TITLE_LEVEL_PREFIXES` for any degree type in `http_404` failures
