---
name: Discovery phase budget guard (Cardiff silent-stall fix)
description: discover_course_links() can burn its whole discovery_phase_timeout_s on failing seed URLs before sitemap fallback ever runs — retries and fallback must check remaining budget, not just their own per-call timeout.
---

## The bug class

A per-page fetch timeout (e.g. `discovery_page_fetch_timeout_s`) bounds a
single fetch, but does nothing to bound the *sum* of sequential fetches
against the outer `discovery_phase_timeout_s` deadline. If N seed URLs each
take close to the per-page timeout to fail (e.g. Cloudflare-blocked host that
times out rather than fast-failing), N sequential seeds can consume nearly
the entire outer budget, leaving downstream stages (retry attempts, sitemap
fallback, alt-listing-path probes) with too little time to even start —
causing a job to "start and stop" with nothing discovered and no useful
error, just a deadline TimeoutError from the orchestrator's outer
`asyncio.wait_for`.

This is a distinct failure mode from "one slow/stuck page" (which a per-page
timeout does fix) — it's "every page is individually bounded but the budget
math for the whole phase was never tracked against a shared deadline."

## The fix pattern

- Track a monotonic deadline once at the top of the discovery function
  (`time.monotonic() + phase_timeout_s`), with a `_remaining_budget_s()`
  helper, instead of only ever checking each stage's own local timeout.
- Derive that internal deadline from the same per-university timeout override
  used by the caller's outer `wait_for`. Extending only the outer timeout is
  ineffective if the crawler's retry/fallback budget still expires at the
  global default.
- Gate optional retries (sleep-then-retry, bare-URL retry) behind
  `remaining_budget > per_page_timeout + reserve` — skip the retry (log a
  warning) once budget is running low, rather than always retrying.
- Before an expensive fallback stage (e.g. sitemap discovery), check
  remaining budget first; skip cleanly with a log/event if there isn't
  enough left, rather than starting it and letting the outer deadline cut it
  off silently. If there is enough budget, still wrap the fallback call in
  `asyncio.wait_for(..., timeout=remaining_budget)` as a second safety net.

## Gap found during regression-testing this fix

Later stages in the same discovery function (alternative-listing-path probe,
subdomain probes) call `fetch_html()` directly with their own `retries=`
kwarg but are **not** wrapped in any deadline-aware `asyncio.wait_for`. In
production `fetch_html` has its own internal timeout so this doesn't hang
forever, but it is still not bounded by the *overall remaining* discovery
budget — the same overrun risk could resurface there for a host that is slow
rather than fast-failing. Not yet fixed; flagged here for the next
regression pass on discovery.py's later stages.
