---
name: JCU Cloudflare Enterprise escalation
description: JCU blocks all transports except Scrape.do render=true; static Scrape.do always 502s ROTATION_FAILED; discovery must go render-first.
---

## ECU contrast
ECU (ecu.edu.au) has a harder failure mode: Scrape.do render=true CAN fetch
individual course pages but the course LISTING (SPA) loads course data via XHR
to ecu-search.funnelback.squiz.cloud which is *separately* CF-protected. Even
12s waitFor returns 0 course links. Fix requires a Funnelback provider (like HUD
SearchStax) that can access the Funnelback subdomain via super=true residential
proxies. External host found in rendered HTML: ecu-search.funnelback.squiz.cloud.
See ecu.yaml for the documented blocker.

## Rule
When a Cloudflare-Enterprise site persistently returns 502 ROTATION_FAILED from
Scrape.do *static* but succeeds with `render=true`, configure the uni YAML with
`scrape_do_render: true` in BOTH `discovery:` and `extraction:` (alongside
`scrape_do_skip_fallbacks: true`). Never rely on the static→render escalation
inside the retry ladder for these sites — static attempts each burn ~60s and
blow the 95s seed-prefetch / 300s discovery budgets.

## Why
- 2026-05: `?international=true` URL rewrite on static HTTP triggered CF bot
  challenge (0 staged) — JS-tab unis need browser/render rescue, not URL rewrite.
- 2026-07: JCU escalated further — plain httpx 403, curl_cffi 403
  "Just a moment...", Scrape.do static 502 ROTATION_FAILED ("cannot connect
  target url", proxy-level connect refusal that retrying never fixes).
  Only `render=true` (residential browser pool) returns 200.
- A previously-working transport tier can silently die; re-verify the live
  transport ladder (curl, curl_cffi, Scrape.do static, Scrape.do render)
  before debugging extractor code.

## How to apply
- `DiscoveryConfig.scrape_do_render` (config/schema.py) → discovery fast-path in
  `http_fetcher.fetch_html` skips the static attempt entirely.
- `ROTATION_FAILED` in a 5xx static response body → `fetch_html_scrape_do`
  fails fast (no backoff retries) so the caller's render tier fires with
  budget remaining. render=True responses keep the normal retry ladder.
- Same pattern family as kingston_2193.yaml / cardiff_2194.yaml (render
  transport), but JCU additionally needed render-first *discovery*.
- Render is ~5-20s/page; cap `max_parallel_fetch` (~4) to avoid saturating
  the residential pool.
- Tests: `tests/test_rotation_failed_fail_fast.py` (6 tests).
