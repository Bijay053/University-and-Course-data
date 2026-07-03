---
name: skip_browser_rescue + scrape_do_skip_fallbacks fetch chain
description: universities that skip both browser rescue and normal httpx/cffi fallbacks lose courses to transient Scrape.do proxy blips; how the retry + Wayback last-resort tiers were added
---

When a university's YAML combines `scrape_do_render: true` +
`scrape_do_skip_fallbacks: true` + `skip_browser_rescue: true` (the standard
config for datacenter-IP-blocked hosts, e.g. Cardiff, QMUL), the
`scrape_do_skip_fallbacks` fast-path in `fetch_html()` becomes the *only*
fetch attempt for every course page — httpx, cffi, and Playwright are all
deliberately skipped because they're blocked too.

**Evolution of the fix (both landed in `http_fetcher.py`'s `scrape_do_skip_fallbacks`
branch):**
1. Originally this fast-path had zero retry. A single transient Scrape.do proxy
   blip (502 / "ROTATION_FAILED", common under concurrent load — same signature
   as the Ulster sitemap issue) on both render AND static permanently lost that
   course. QMUL job_5f5ab180197a lost 47/409 courses (~11%) to this gap.
   Fix: retry render once after a 3s backoff if render+static both fail.
2. The retry cut losses to ~5% (7/125, job_aba92c0d3316) but didn't eliminate
   them, because all 3 attempts still go through the *same* Scrape.do proxy
   pool — a pool-wide blip can fail all 3 together. Fix: added a final
   Wayback Machine (`fetch_html_wayback`) attempt after the retry also fails,
   before giving up. Archive.org is not behind the university's live WAF at
   all, so it's a genuinely independent last-resort tier. Only fires after 3
   Scrape.do attempts already failed, so the extra round-trip cost is
   negligible.

**How to apply:** if a skip_browser_rescue university still shows a residual
`fetch_failed` tail after both fixes, check whether Wayback Machine actually
has an archived snapshot for those specific course URLs (brand-new/renamed
pages genuinely may have none) before assuming the code is still broken —
that would be a site-side data gap, not a fetch-chain gap.

**Diagnostic gotcha:** `log.info`/`log.warning` calls inside `fetch_html()`
(the Python `logging` module) do NOT appear in the emit()-based job status
log that gets pasted into chat — only `[STAGE]`/`[BROWSER↑ SKIPPED]`/etc
emit() lines do. Grepping a pasted job log for scrape.do branch names will
show zero hits even when that code path fired; you need the raw Celery
worker log (`/tmp/logs/backend-py_Celery_worker_*.log`, which rotates) to see
what actually ran.

**Celery does not hot-reload — a real gap this bug already exposed once.**
The FastAPI workflow runs with `--reload`, so editing files under
`backend-py/app/services/scraper/` takes effect immediately for API routes.
The Celery worker workflow has no `--reload` flag and forks its pool at
startup, so it keeps running the pre-edit code until the "backend-py: Celery
worker" workflow is explicitly restarted — one scrape run after this exact
fix landed still showed the old zero-retry behavior (0 occurrences of the new
log line) purely because the worker process predated the file edit. Always
restart that workflow (not just verify the file diff) after any change under
`app/services/scraper/`, and confirm via `ps -o lstart` vs the file's mtime,
or by grepping the fresh worker log for a marker unique to the new code.
