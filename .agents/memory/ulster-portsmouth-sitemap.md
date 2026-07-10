---
name: Ulster & Portsmouth sitemap discovery
description: How to configure Cloudflare-blocked UK universities (Ulster, Portsmouth) to use sitemap supplement for full course discovery
---

## Ulster University (ulster_2176.yaml)

**Problem**: BFS finds ~38 /study/* nav links (these match _COURSE_URL_HINTS because /study/ is a hint). 38 ≥ threshold(5), so the automatic sitemap fallback never fires → 0 courses staged.

**Fix**: `always_sitemap_supplement: true` forces sitemap unconditionally regardless of BFS count.

**Sitemap**: `https://www.ulster.ac.uk/site-maps/sitemap-courses.xml` — 987 URLs at `/courses/202627/<slug>-<id>`. Accessible via Scrape.do (direct HTTP returns 403). Individual course pages also blocked by Cloudflare → fall back to Wayback Machine (5-10s per page).

**allow_url_patterns**: `ulster\.ac\.uk/courses/\d{6}/` — blocks the 38 /study/* nav links while passing all sitemap course URLs.

**max_candidates**: Must be 1000+ (default 200 caps well below the 987 in sitemap).

**Warning**: `always_sitemap_supplement: true` must be in the YAML BEFORE the job starts — it's loaded at job start time, not at trigger time.

## Portsmouth University (port_2174.yaml)

**Sitemap**: `https://www.port.ac.uk/sitemap.xml?page=2` — 419 top-level course URLs (depth-4 pattern: `/study/courses/<type>/<slug>`). Direct HTTP returns 200 OK for both sitemap and individual pages.

**allow_url_patterns**: `port\.ac\.uk/study/courses/[^/]+/[^/]+/?$` — exactly 2 segments after /study/courses/ for top-level only. Excludes sub-pages.

**max_candidates**: 500 (sufficient for 419 sitemap + BFS candidates).

## Orchestrator render_listing_pages filter bug (fixed)

`_path` (relative path) was used where `_abs` (full URL) was needed at orchestrator.py ~line 1768. `allow_url_patterns` uses `re.search(pattern, full_url)` but the filter was checking only the path component → ALL links were dropped when `allow_url_patterns` included a hostname pattern. Fix: use `_abs`.

18 unit tests in `backend-py/tests/test_render_listing_pages_filter.py`.

## Deduplication guard

Starting a new scrape while one is already running for the same university_id returns the EXISTING running job ID (no new job created). Must wait for job N to complete before triggering job N+1 for the same university.

## Sequential extraction bottleneck

Each course takes ~70s end-to-end (page fetch + extraction + AI fallback + snapshot upload). With 8 Celery workers and universities running on separate workers, one job processes its university sequentially. Approximate runtimes:
- 200 courses (Ulster job 3): ~4 hours
- 419 courses (Portsmouth job 2): ~6 hours  
- 987 courses (Ulster job 4): ~19 hours

Plan accordingly — these jobs cannot be monitored within a single session.

**Why:** Celery `--concurrency=8` = 8 worker processes. Each university scrape runs on one process. Within that process the asyncio extraction loop has very low concurrency (effectively ~1-2 courses at a time due to Wayback/Scrape.do rate limits and Gemini AI sequential calls).

## Sitemap probe outer-timeout vs. discovery-fast-path retry chain (2026-07-03)

**Symptom**: discovery.scrape_do_skip_fallbacks=True hosts (Ulster) suddenly regressed from ~987 to ~33 courses — sitemap log showed "fetch returned 0 byte(s)" even though the sitemap URL and YAML config were correct and the discovery fast-path's static→render retry code was in place and running.

**Root cause**: `sitemap.py`'s `_fetch_text()` wraps the whole `fetch_html()` call (which internally does static→502→render-retry) in an outer `asyncio.wait_for(..., timeout=_PROBE_TIMEOUT_S)`. When Scrape.do's static/residential pool is slow to fail over (observed: ~58s to 502 before the render retry succeeds in ~9s, ~67s total), a 15s outer timeout kills the whole chain before the render retry ever runs — so it looks identical to "no sitemap" even though the fetch would succeed given time.

**Fix**: raised `_PROBE_TIMEOUT_S` in `sitemap.py` from 15.0 to 100.0 to give the full static+render retry room to complete.

**How to diagnose this class of bug**: don't trust the browser-facing job log alone — it's built from `emit()` calls only. The `discovery.scrape_do_skip_fallbacks=True` / retry / failure lines are `log.info`/`log.warning` calls to the Python logger, only visible in the raw Celery worker log (or by re-running the fetch in isolation via a one-off script that calls `get_config_for_host()` + `set_uni_config()` + the fetch function directly). Reproducing in isolation is the fastest way to see the true timing breakdown.

## Second regression (2026-07-10): 987→35→38 courses, `_PROBE_TIMEOUT_S` bump alone was insufficient

**Symptom**: after the 2026-07-03 fix above, Ulster regressed again to 35-38 courses. Raising `_PROBE_TIMEOUT_S` to 150s still wasn't enough — the sitemap fetch timed out on attempt 0 with no visible retry.

**Root cause (deeper layer)**: `fetch_html_scrape_do()` has its OWN internal exponential-backoff retry ladder (2s/8s/30s = up to 4 attempts) for any 429/500/502/503/504 response, independent of the discovery fast-path's outer static→render retry. On a host where the static leg is *known-doomed* (Cloudflare Enterprise always 502s it), that internal ladder burns ~40s+ retrying a call that will never succeed, before the code even falls through to the render=True leg — which then burns its own ~40s+ ladder. Two full doomed-then-working ladders easily exceed even a 150s outer timeout when Scrape.do's proxy pool is degraded.

**Fix**: added `max_retries: int | None = None` param to `fetch_html_scrape_do()` (`http_fetcher.py`) — when set, truncates the internal `_SD_BACKOFFS` tuple. The discovery fast-path's static-leg call (in `fetch_html()`, the `scrape_do_skip_fallbacks` branch) now passes `max_retries=0` so the doomed static attempt fails after one shot instead of four, preserving the outer timeout budget for the render=True leg (which keeps its full default retry ladder).

**Lesson**: when a retry ladder isn't behaving as expected, check for a SECOND, independent retry ladder nested one layer deeper — `fetch_html()`'s own static→render retry and `fetch_html_scrape_do()`'s internal backoff loop look like one system from the caller's side but stack multiplicatively.

## Third regression (2026-07-10): discovery cache poisoning + render-ladder still didn't fit budget

**Symptom**: even after the `max_retries=0` static-leg fix above shipped and was verified once (job found 566), Ulster kept reporting 38 courses on every subsequent run, including ones without `forceDiscovery`.

**Root cause A — discovery cache poisoning**: the C1 7-day `discovery_url_cache` table (keyed by `university_id`) had cached `link_count=38` from a bad run that predated the fix. Since normal (non-`forceDiscovery`) runs skip discovery entirely and reuse the cached count, every run kept replaying the stale 38 forever regardless of code fixes. **Any "found way fewer than expected" report must include checking/deleting the relevant `discovery_url_cache` row** — a code fix alone produces zero visible effect until the poisoned cache entry is cleared.

**Root cause B — render ladder still didn't fit the outer probe budget**: after clearing the cache, `forceDiscovery=true` re-ran real discovery and exposed a second bug: the discovery fast-path's render=True leg (in `fetch_html()`) still used the *default* 4-attempt retry ladder. On this host, Scrape.do's render tier was taking ~57-60s per attempt to fail (not a fast rejection) — real, in-flight latency — so 4 attempts need 250s+, but `sitemap.py`'s outer `_PROBE_TIMEOUT_S` is 150s. The probe was killed mid-attempt every time, discarding a real (failing) response instead of letting the ladder complete on its own terms. Fixed by passing `max_retries=1` (2 total attempts, ~60s+2s backoff+~60s ≈ 122s) to that specific render call so it fits inside 150s.

**Then discovered: genuine Scrape.do-side outage for this host.** With the retry-ladder now correctly using its full (smaller) budget, BOTH static and render legs still returned 502 `ROTATION_FAILED` — confirmed independently outside the app by calling the Scrape.do API directly with `super=true` and `super=true&render=true`, both still 502. No Wayback snapshot of the sitemap XML exists either. This is not fixable client-side; it requires Scrape.do's proxy pool for this host to recover.

**Stopgap applied**: raised `discovery.browser_time_budget_s` to 240 (default 90) in `ulster_2176.yaml` so the nav-based `browser_discover_generic` fallback (which fires when the sitemap fetch fails) covers more of the nav tree before cutting off — improved 38→62 courses in one test run. This is structurally still incomplete vs. the full ~566-course sitemap catalog (nav discovery can only find what's linked from crawled listing/subject pages) and should be reverted once the sitemap path is confirmed healthy again — don't leave an inflated browser budget as permanent config for a host that normally relies on the sitemap. **Also**: a partial-but-"healthy" (≥5 links) browser-fallback run still gets written to `discovery_url_cache` and will re-poison it for 7 days — delete the cache row again after any such fallback run before considering the incident closed.
