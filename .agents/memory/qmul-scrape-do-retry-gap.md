---
name: skip_browser_rescue + scrape_do_skip_fallbacks has no retry tier
description: universities that skip both browser rescue and normal httpx/cffi fallbacks lose courses to transient Scrape.do proxy blips with zero recourse
---

When a university's YAML combines `scrape_do_render: true` +
`scrape_do_skip_fallbacks: true` + `skip_browser_rescue: true` (the standard
config for datacenter-IP-blocked hosts, e.g. Cardiff, QMUL), the
`scrape_do_skip_fallbacks` fast-path in `fetch_html()` becomes the *only*
fetch attempt for every course page — httpx, cffi, Wayback, and Playwright are
all deliberately skipped because they're blocked too.

**Why this matters:** unlike every other fallback chain in the fetcher, this
fast-path had no retry. A single transient Scrape.do proxy blip (502 /
"ROTATION_FAILED", common under concurrent load — same signature as the
Ulster sitemap issue) on both render AND static permanently lost that course,
with no other tier to fall back to. QMUL job_5f5ab180197a lost 47/409 courses
(~11%) to exactly this gap.

**How to apply:** when adding this 3-flag combo for a new blocked university,
remember the fast-path itself now retries render once after a backoff if
render+static both fail on the first pass (`http_fetcher.py`, inside the
`scrape_do_skip_fallbacks` branch). If a university still shows a high
fetch_failed rate after this, the block is likely permanent for those specific
URLs (not transient) and needs a different fix (e.g. a different Scrape.do geo
code, or the university genuinely needs `skip_browser_rescue: false`).

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
