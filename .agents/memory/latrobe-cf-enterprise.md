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

## One-session extraction through Cloudflare

**Rule:** Use one rendered Scrape.do browser session per course. Preserve the
course shell, read its `allDetailUrls` manifest, then top-level-navigate that same
browser to the canonical international JSON. Decode Chromium's HTML-escaped
`<pre>` response without running the generic embedded-HTML unescaper over the
combined wrapper.

**Why:** Direct/static requests fail, and an in-page `fetch()` from the rendered
course returns 403. A top-level browser navigation succeeds. Separate course and
JSON renders double provider traffic, while the generic unescaper corrupts JSON
that contains a full HTML document.

**How to apply:** Preserve the shell across navigation, return the selected
detail URL with the prefetched document, and trust it only when it exactly
matches the Python authority selector. Any missing, malformed, or mismatched
prefetch must use a rendered canonical-detail fallback, never plain HTTP.

## Canonical variant selection

**Rule:** Browser and Python selectors must use the same order: earliest
published numeric year, then campus `CI` > `BU` > `ON` > `SY` > first available.

**Why:** Different year/campus choices can carry different fees, durations, and
entry requirements; silently pairing a later-year prefetched document with an
earlier-year canonical URL stages incorrect data.

**How to apply:** Keep selector-parity tests and URL-equality validation. Treat
any mismatch as a fallback condition rather than accepting the prefetched JSON.

## Concurrency for the sub-30-minute target

**Rule:** Keep the shared Scrape.do default at five slots, but allow La Trobe
eight in-flight provider requests with enough queued course tasks to saturate
them.

**Why:** Five slots measured about seven courses/minute and missed the target.
Eight slots measured roughly 9–11 courses/minute, projecting 232-course
extraction around 22–27 minutes without raising the global limit for other
universities.

**How to apply:** Scope the higher semaphore to La Trobe's validated config.
Do not increase the global default or remove retry/backoff protections.
