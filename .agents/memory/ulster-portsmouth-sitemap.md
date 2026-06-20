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
