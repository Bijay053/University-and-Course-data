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
