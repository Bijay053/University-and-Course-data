---
name: Ulster & Portsmouth sitemap discovery
description: How to configure Cloudflare-blocked UK universities (Ulster, Portsmouth) to use sitemap supplement for full course discovery
---

## Ulster University (ulster_2176.yaml)

**Problem**: BFS finds ~38 /study/* nav links (these match _COURSE_URL_HINTS because /study/ is a hint). 38 ≥ threshold(5), so the automatic sitemap fallback never fires → 0 courses staged.

**Fix**: `always_sitemap_supplement: true` forces sitemap unconditionally regardless of BFS count.

**Sitemap**: `https://www.ulster.ac.uk/site-maps/sitemap-courses.xml` — 1050 URLs at `/courses/202627/<slug>-<id>` + `/courses/202728/<slug>-<id>`. Accessible via Scrape.do (direct HTTP returns 403). Individual course pages also served via Scrape.do static (249KB SSR HTML; Cloudflare Enterprise blocks httpx/cffi/render/Playwright).

**allow_url_patterns**: `ulster\.ac\.uk/courses/\d{6}/` — blocks the 38 /study/* nav links while passing all sitemap course URLs.

**max_candidates**: Must be 1500+ (sitemap has 1050 raw URLs).

**block_url_patterns**: `/courses/202728/` (future year, incomplete), `pgce`, `degree-apprenticeship`.

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

Each course takes ~4s end-to-end via Scrape.do static (max_parallel_fetch: 4 for Ulster). Approximate runtimes for Ulster:
- 265 courses: ~35 min
- 575 courses: ~45-60 min

## Sitemap probe outer-timeout vs. discovery-fast-path retry chain (2026-07-03)

**Symptom**: discovery.scrape_do_skip_fallbacks=True hosts (Ulster) suddenly regressed from ~987 to ~33 courses — sitemap log showed "fetch returned 0 byte(s)" even though the sitemap URL and YAML config were correct.

**Root cause**: `sitemap.py`'s `_fetch_text()` wraps the whole `fetch_html()` call in an outer `asyncio.wait_for(..., timeout=_PROBE_TIMEOUT_S)`. When Scrape.do's static pool is slow to fail over (~58s to 502 before the render retry succeeds in ~9s, ~67s total), a 15s outer timeout kills the whole chain before the render retry runs.

**Fix**: raised `_PROBE_TIMEOUT_S` in `sitemap.py` from 15.0 to 100.0.

## Second regression (2026-07-10): 987→35→38 courses

**Root cause**: `fetch_html_scrape_do()` has its OWN internal exponential-backoff retry ladder (2s/8s/30s = up to 4 attempts). On a host where the static leg is doomed (Cloudflare Enterprise always 502s it), that internal ladder burns ~40s+ retrying before falling through to render=True leg. Two full ladders easily exceed even a 150s outer timeout.

**Fix**: added `max_retries: int | None = None` param to `fetch_html_scrape_do()` (`http_fetcher.py`). The static-leg call in the `scrape_do_skip_fallbacks` branch now passes `max_retries=0` so it fails after one shot.

## Third regression (2026-07-10): discovery cache poisoning + render-ladder still didn't fit budget

**Root cause A — discovery cache poisoning**: `discovery_url_cache` had cached `link_count=38` from a bad run. Every non-`forceDiscovery` run replayed the stale count. **Any "found fewer than expected" report must check/delete the `discovery_url_cache` row first.**

**Root cause B — render ladder didn't fit outer probe budget**: render=True leg still used the default 4-attempt ladder. ~57-60s per attempt × 2 = ~122s. Fixed by passing `max_retries=1` (2 total attempts).

**Then: genuine Scrape.do outage confirmed**. Both static and render legs returned 502 `ROTATION_FAILED` even with `super=true`. Not fixable client-side.

## Fourth regression / Funnelback harvest approach (2026-07-13)

**Symptom**: sitemap URL has been returning 502 ROTATION_FAILED for multiple days. Browser fallback with 240s budget only finds 62 courses.

**Root cause**: Scrape.do proxy pool has a sustained outage specifically for the sitemap XML URL (`/site-maps/sitemap-courses.xml`). Regular Ulster pages still work (Scrape.do render=true returns 200 for `/courses`).

**Investigation findings**:
- `www.ulster.ac.uk/courses` uses Funnelback DXP v16 (Squiz Cloud collection `ulster~sp-courses`)
- Course results are embedded in the rendered HTML as `squiz.cloud/s/redirect?...&url=<course-url>&...` links
- These redirect hrefs use HTML entity encoding (`&amp;`) so naive URL extraction fails
- Scrape.do render=true with `waitFor=8000` retrieves all 100 courses per page
- 11 pages needed for ~1060 total URLs (601 in 202627, 459 in 202728)
- After blocking 202728/pgce/degree-apprenticeship: **575 candidates**

**Fix**: `discovery.static_course_urls_file` option added to `DiscoveryConfig` in `schema.py` and orchestrator. When set, reads pre-harvested course URLs from a text file (one per line, `#` comments ignored), applies `block_url_patterns`, and uses them directly as candidates — bypassing all other discovery tiers.

**Harvest script**: `backend-py/scripts/harvest_ulster_urls.py` — re-run when the URL list goes stale.
**URL file**: `backend-py/scraper_config/unis/ulster_2176_course_urls.txt` (1060 lines, committed to git).

**To restore sitemap path** when Scrape.do recovers: remove `static_course_urls_file:` line from `ulster_2176.yaml` — the existing `sitemap_url:` config takes effect automatically.

**How to diagnose Scrape.do outage vs. code bug**:
1. Test direct: `curl -s -o /dev/null -w "%{http_code}" https://www.ulster.ac.uk/site-maps/sitemap-courses.xml` → 403 (Cloudflare)
2. Test cffi: `python3 -c "from curl_cffi import requests as r; print(r.get('...', impersonate='chrome120').status_code)"` → 500 (cffi fails)
3. Test Scrape.do static: API call with `render=false` → 502 = outage
4. Test Scrape.do render: API call with `render=true` → 502 = full outage for this host
5. Test regular Ulster page via Scrape.do render: if this returns 200, outage is SITEMAP-URL-SPECIFIC
