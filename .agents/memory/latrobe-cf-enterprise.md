---
name: La Trobe CF Enterprise scraper fixes
description: Full fix stack for La Trobe University (uni_id=21) Cloudflare Enterprise + Adobe Target prehide shell scraping quirks
---

## Fix stack (all in scraper_config/unis/latrobe.yaml)

**Discovery:**
- `scrape_do_render: true` + `scrape_do_skip_fallbacks: true` — CF Enterprise blocks all other transports
- `sitemap_url` — pointing directly to sitemap index bypasses BFS deadlock

**Extraction:**
- `scrape_do_render: true` + `scrape_do_skip_fallbacks: true` — same CF reason
- `skip_browser_rescue: true` — datacenter Playwright = CF 403
- `skip_per_course_browser: true` — per-course browser fee extraction path also CF blocked; without this flag every batch stalls 60-120s before saves
- `skip_degree_qualifier_check: true` — La Trobe offers "Professional Certificate" / "Undergraduate Certificate" (not in `_DEGREE_QUALIFIER_RE`); Scrape.do also sometimes returns Adobe Target prehide shell with no H1, making even "Juris Doctor" trip the guard
- `max_parallel_fetch: 3` — 1 is too slow (33+ min/batch); 3 gives ~22 min/batch
- `online_only.enabled: false` — prehide shell has no location text → `no_location_online_override` stamps study_mode="Online" → online_only filter rejects all courses
- `require_international_fee: false` — fees behind JS campus/student-type selector, not extractable automatically

## Key timing insight
`asyncio.gather` collects ALL results before the `for r in results:` staging loop runs. With `max_parallel_fetch: 1` and 100 courses/batch at ~40s each: saves=0 for 33+ minutes. Raise to ≥3 so batch completes in ~22 min.

## Results (completed run)
Found:224 | Staged:175 (after two runs) | Skipped:49 domestic_only | FetchFailed:0 | Errors:0

**Why:** CF Enterprise + Adobe Target are La Trobe's full JS rendering stack. No plain httpx/cffi/datacenter-Playwright transport works.
