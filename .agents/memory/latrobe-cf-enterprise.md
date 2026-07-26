---
name: La Trobe CF Enterprise + Funnelback AJAX scraper fixes
description: Full fix stack for La Trobe University (uni_id=21) — CF Enterprise, Adobe Target prehide, and Funnelback AJAX-loaded search results
---

## Fix stack (all in scraper_config/unis/latrobe.yaml)

**Discovery:**
- `scrape_do_render: true` + `scrape_do_skip_fallbacks: true` — CF Enterprise blocks all other transports
- `scrape_do_wait_for_ms: 8000` — **CRITICAL**: La Trobe's Funnelback search results are AJAX-loaded AFTER the initial page render. Default 3000ms is too short; every seed page captures only the 9 navigation template links (same on all 40 pages → +0 new candidates per page). 8000ms gives the AJAX enough time to populate the result cards (~10 course links per page).
- 40 Funnelback seed URLs (start_rank=1..391, step=10, NO `num_ranks` param)
- `discovery_phase_timeout_s: 600` — 40 seeds at 4 concurrent × (12s render + 8s wait) ≈ 200s; 600s cap gives headroom
- `skip_sitemap_fallback: true` — sitemap covers only ~215 of 395 courses (undercounts)
- `bfs_page_budget: 50` — must be ≥ 40 (number of seeds)

**Extraction:**
- `scrape_do_render: true` + `scrape_do_skip_fallbacks: true` — same CF reason
- `skip_browser_rescue: true` — datacenter Playwright = CF 403
- `skip_per_course_browser: true` — per-course browser fee extraction path also CF blocked; without this flag every batch stalls 60-120s before saves
- `skip_degree_qualifier_check: true` — La Trobe offers "Professional Certificate" / "Undergraduate Certificate" (not in `_DEGREE_QUALIFIER_RE`); Scrape.do also sometimes returns Adobe Target prehide shell with no H1, making even "Juris Doctor" trip the guard
- `max_parallel_fetch: 3` — 1 is too slow (33+ min/batch); 3 gives ~22 min/batch
- `online_only.enabled: false` — prehide shell has no location text → `no_location_online_override` stamps study_mode="Online" → online_only filter rejects all courses
- `require_international_fee: false` — fees behind JS campus/student-type selector, not extractable automatically

## Funnelback seed pagination — critical lessons

**The Funnelback AJAX wait is the #1 La Trobe discovery issue.**
- La Trobe's `/search?...` page renders the template immediately but fires an AJAX call to populate result cards. With the default 3s wait, Scrape.do captures 0 result cards.
- Evidence: "9 course links found" on EVERY seed page, ALL the same links (+0 new candidates from page 2 onwards) → those 9 are the navigation template, not results.
- Fix: `scrape_do_wait_for_ms: 8000` in the discovery section.

**Do NOT add `num_ranks=N` to Funnelback seed URLs.**
- Adding `num_ranks=20` broke the La Trobe Funnelback result page format: the server returned 0 `/s/redirect?` course links → `discovered=1`, `staged=0`. Reverted.
- The server default (10 results/page) is correct. Use more seed pages (step=10) instead of bigger pages.

**Per-uni discovery timeout (`discovery_phase_timeout_s`):**
- Added `DiscoveryConfig.discovery_phase_timeout_s: Optional[int]` to schema.py.
- orchestrator.py uses `getattr(_uni_cfg.discovery, "discovery_phase_timeout_s", None) or settings.discovery_phase_timeout_s` for the `asyncio.wait_for()` call.
- La Trobe sets 600 s.

## Key timing insight
`asyncio.gather` collects ALL results before the `for r in results:` staging loop runs. With `max_parallel_fetch: 1` and 100 courses/batch at ~40s each: saves=0 for 33+ minutes. Raise to ≥3 so batch completes in ~22 min.

**Why:** CF Enterprise + Adobe Target are La Trobe's full JS rendering stack. No plain httpx/cffi/datacenter-Playwright transport works. Funnelback search results are AJAX-loaded and need `scrape_do_wait_for_ms: 8000` to appear in the Scrape.do capture.
