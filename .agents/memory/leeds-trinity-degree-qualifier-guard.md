---
name: Leeds Trinity degree-qualifier guard false positive
description: SearchStax links_only universities can have plain subject-only course titles that trip the generic category-landing-page guard
---

Leeds Trinity (like ARU/Writtle before it) publishes course pages with plain
subject titles as the H1 — "Criminology", "Film", "Games Design" — with no
"BA (Hons)"-style degree-level prefix. `should_stage_course()`'s
`_name_has_degree_qualifier` check treats any title lacking a degree word as
a category/landing page and rejects it with
`category_landing_page_missing_degree_qualifier`.

**Why:** the guard exists to filter real category hub pages, but it assumes
every real course title carries a degree-level qualifier word. Universities
that omit the qualifier from the page H1 get almost all of their real courses
silently rejected (Leeds Trinity: 127/132 rejected, only 5 staged) even
though discovery and URL-scoping worked correctly.

**How to apply:** when a newly-onboarded/fixed university stages far fewer
courses than discovery found (staged << total_found) with errors=0, check the
Celery log for `staging_gate rejected ... category_landing_page_missing_degree_qualifier`
before assuming a discovery/auth bug. If confirmed, add
`extraction.staging.skip_degree_qualifier_check: true` to the per-uni YAML
(same opt-out already used for ARU/Writtle) rather than touching the shared
guard. Re-run the scrape after a Celery worker restart to confirm the ratio
of staged-to-discovered jumps back to normal (Leeds Trinity: 123/132 after fix).

## Follow-up: missing degree-qualifier prefix + gemini_primary academic_level overwrite (2026-07-10)

Even with staging fixed, LTU course *names* and `degree_level`/`academic_level`
were still wrong: the page H1 is bare subject text ("Nursing (Mental Health)")
with the real qualifier ("BSc (Hons)") living in a separate banner sub/lead
element, not the H1 itself — generic name/degree_level classifiers never see it.

**Fix:** added a structural pre-pass (`_from_ltu_banner()` in both
`course_name.py` and `degree_level.py`) that reads the BEM-scoped banner
classes (`.banner-title__sub`, `.banner-title__lead`, `h1.banner-title__main`)
directly, before falling back to the generic classifiers. Safe because these
classes are LTU-specific — no risk to shared/generic guard logic.

**Separate bug found in the same investigation:** `gemini_primary` in
`single_course.py` was unconditionally overwriting the correct structural
`academic_level` ("Undergraduate") with a wrong Gemini classification ("Year
12"), logged as `[FIELD_OVERWRITE] ... academic_level from 'Undergraduate' ->
'Year 12'`. Root cause: `_STRUCTURAL_COURSE_PAGE_PREFIXES` (the allow-list of
extraction-method prefixes gemini_primary is not allowed to clobber) was
missing `"degree_level:"`, so anything produced by `degree_level.*` methods
(including the new banner pre-pass) was fair game for gemini overwrite.

**Why this matters generally:** any new structural extractor module whose
method-name prefix isn't already in `_STRUCTURAL_COURSE_PAGE_PREFIXES` is
silently overwritable by `gemini_primary`, even when it just produced a
100%-correct value. When adding a new extractor family, check whether its
`extraction_method` prefix needs to be added to that tuple.

**Verified end-to-end (dev, 2026-07-10):** fresh forced-discovery rescrape,
job imported 123/132 (9 skipped, 0 errors). All Nursing pathway titles now
read `BSc (Hons) Nursing (Adult/Child/Learning Disabilities/Mental Health)`
with degree_level `Bachelor's` / academic_level `Undergraduate`; MA/PG
courses correctly `Master's`/`Postgraduate`. No further `[FIELD_OVERWRITE]`
on academic_level observed in the run.
