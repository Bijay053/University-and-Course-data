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
