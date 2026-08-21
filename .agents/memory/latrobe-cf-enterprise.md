---
name: La Trobe CF Enterprise + Funnelback discovery fix
description: Full fix stack for La Trobe University (uni_id=21) — CF Enterprise, Adobe Target prehide, and the correct Funnelback discovery endpoint
---

## Discovery fix — use Squiz Cloud direct endpoint (NOT latrobe.edu.au/search?)

**The critical lesson:** La Trobe has TWO different Funnelback entry points:

1. `https://www.latrobe.edu.au/search?...` — a CUSTOM PAGE that wraps Funnelback via JavaScript AJAX. Results load AFTER the initial render → scrape_do_wait_for_ms=8000 is unreliable → every seed page returns the same 9 nav template links, +0 new candidates from page 2 onwards.

2. `https://latr-search.funnelback.squiz.cloud/s/search.html?...` — the DIRECT Funnelback search server. Results are in the INITIAL HTML (server-side rendered), no AJAX. This is the correct endpoint.

**Correct seed URL format:**
```
https://latr-search.funnelback.squiz.cloud/s/search.html?f.Tabs%7Clatr%7Eds-courses=Courses&query=&searchtype=global&collection=latr%7Esp-latrobe&start_rank=1&num_ranks=50
```
- 8 seeds × num_ranks=50 = 400 slots for 395 courses
- step=50 (start_rank 1, 51, 101, ..., 351)
- Funnelback redirect links: `/s/redirect?...&url=https%3A%2F%2Fwww.latrobe.edu.au%2Fcourses%2F...`
  → `_unwrap_funnelback_redirect()` converts to La Trobe URLs → pass origin check ✓

**Wrong approaches tried:**
1. `num_ranks=20` on latrobe.edu.au/search? — broke Funnelback HTML format → 0 course links
2. `scrape_do_wait_for_ms: 8000` on latrobe.edu.au/search? — unreliable, same 9 nav links every page
3. Using latrobe.edu.au/search? at all — that page is AJAX-loaded, Squiz Cloud is server-side

## Cross-origin BFS extraction (discovery.py _extra_candidate_origins)

**The second bug:** even with the squiz.cloud seeds, the Python BFS in `discover_course_links()` was
returning 0 candidates. Root cause: `_resolve(href, base=squiz_url, origin=squiz_origin)` drops any link
whose resolved URL doesn't start with `squiz_origin`. Funnelback redirect hrefs like
`/s/redirect?...&url=https%3A%2F%2Fwww.latrobe.edu.au%2Fcourses%2F...` unwrap to `www.latrobe.edu.au`
— a DIFFERENT origin — so they were silently discarded.

**Fix in discovery.py:**
1. `discover_course_links()` now accepts `scrape_url` (the university's main domain URL).
2. `_extra_candidate_origins` tuple is set when `scrape_url` origin ≠ BFS seed origin.
3. After the normal `_resolve()` link-extraction pass, a second pass scans `ext.links` for
   `/s/redirect` hrefs, unwraps them via `_unwrap_funnelback_redirect()`, checks the unwrapped
   URL starts with an extra_candidate_origin, applies block/allow/looks_like_course filters,
   then adds to `found` directly (never to BFS queue — no need to crawl latrobe.edu.au itself).
4. `orchestrator.py` updated to pass `scrape_url=scrape_url` to `discover_course_links()`.

**Result:** Python BFS finds 191 courses from 7/8 pages (page 8 had a transient scrape.do failure;
if it succeeded, expected ~228). Previously found 0 from Python BFS.

**Stale-code trap (Celery forked processes):** The fix was on disk but the first Celery restart still
ran 0-course jobs. The second restart (after adding a diagnostic log emit) picked up fresh code.
If a recently-edited fix still shows the old behaviour: check the Celery worker process start time vs
commit/edit time before re-debugging the fix itself. A `restart_workflow` for the Celery worker must
fully kill all forked workers — if the logs still show old behaviour after the first restart, do a
second restart.

## Discovery YAML settings (latrobe.yaml)
- `scrape_do_render: true` + `scrape_do_skip_fallbacks: true` — squiz.cloud also has CF
- `discovery_phase_timeout_s: 600` — safety margin (actual: 8/4 concurrent × 30s = 60s)
- `bfs_page_budget: 15` — only 8 seeds needed
- `max_candidates: 450` — above 395
- NO `scrape_do_wait_for_ms` override — default 3000ms fine for server-side HTML
- `always_browser_discover` NOT set — defaults False; Python BFS finds ~191-228 courses

## Per-page yield from Funnelback
Each 50-course Funnelback page passes ~30-40 through `_looks_like_course()`. Non-qualifying links are
discipline hub pages (e.g. `/courses/accounting`) — correct rejections, not bugs.

## Extraction fix stack (latrobe.yaml)
- `scrape_do_render: true` + `scrape_do_skip_fallbacks: true` — CF Enterprise blocks all other transports on latrobe.edu.au
- `skip_browser_rescue: true` + `skip_per_course_browser: true` — datacenter Playwright = CF 403; per-course browser path stalls 60-120s/batch
- `skip_degree_qualifier_check: true` — Adobe Target prehide shell has no H1; "Professional Certificate" etc. not in `_DEGREE_QUALIFIER_RE`
- `max_parallel_fetch: 3` — 1 is too slow; 3 ≈ 22 min/batch
- `online_only.enabled: false` — prehide shell → no location text → online_only filter rejects all
- `require_international_fee: false` — fees behind JS selector
- `domestic_only.enabled: true` — filter domestic-only courses

## Key timing insight for extraction
`asyncio.gather` collects ALL results before staging loop. `max_parallel_fetch: 1` at 40s/course = 33+ min/batch before any saves. Must use ≥ 3.

**Why squiz.cloud endpoint is correct:** The squiz.cloud domain IS the Funnelback search engine. La Trobe's own search page is just a JS wrapper that AJAX-loads from squiz.cloud — it doesn't have the results in its initial HTML.

## Rendered JSON and the sub-30-minute path

Scrape.do `render=true` opens La Trobe's international detail JSON in Chromium,
which returns an HTML document whose `<pre>` contains the HTML-escaped JSON.
Treating the whole response as JSON makes every authoritative override fail even
though fee, duration, intake, location, and English requirements are present.

**Rule:** Unwrap and HTML-decode the `<pre>` payload before JSON parsing. Once
that authoritative response works, do not fetch the separate entry-requirements
SPA tab or repeat the primary render just because the template has no visible H1.

**Why:** The old route made roughly four paid renders per course (primary,
doomed H1 retry, English tab, detail JSON), yet rejected the only response with
complete structured data. At three courses in flight, 232 courses projected at
55–80 minutes.

**How to apply:** Keep one short primary render to obtain the detail manifest,
then one detail-JSON request. Eight courses in flight is the controlled target
for this host; it removes redundant calls rather than relying only on more load.
