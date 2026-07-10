---
name: Static single-page listing undercounted by BFS heuristics
description: When a fully static /courses listing page contains all course links but BFS only finds a fraction, the generic category-landing classifier is likely misclassifying valid detail-page slugs.
---

Symptom: a university's course-listing page is one static HTML page (no pagination, no JS) containing every course link, but default BFS discovery only surfaces a small subset (e.g. 33 of 121+).

**Why:** the generic classifier treats short/ambiguous slugs (acronyms, no degree-qualifier words) as category-landing/nav noise rather than course-detail pages, so most real links never enter the candidate set even though BFS visits the listing page fine.

**How to apply:** don't add BFS budget or browser rendering — the page is already fully fetched. Instead pin `discovery.seed_urls` to the listing page and set matching regexes on `allow_url_patterns` (bypass BFS visit-worthiness checks) + `force_candidate_url_patterns` (bypass the category-landing classifier) + `course_detail_url_patterns` (final extraction gate) — all to the same course-detail URL shape. Explicitly exclude non-course paths under the same prefix (e.g. `/courses/search`) with a negative lookahead, since a broad `[a-z0-9-]+` slug pattern will otherwise match them and stage junk "courses".
