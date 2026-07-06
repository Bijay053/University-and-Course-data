---
name: Scrape.do discovery timeout mismatch (Cardiff "doesn't scrape at all")
description: When a per-uni config routes discovery-phase fetches through Scrape.do, the generic short per-page timeout can silently discard every legitimately-slow-but-successful response, producing 0 discovered links with no error.
---

## The bug class

A discovery/crawl function can have two independent timeout layers that were
tuned for different fetch paths:

- A generic per-page timeout (e.g. `discovery_page_fetch_timeout_s`, tuned for
  plain httpx/curl_cffi calls that normally return in a few seconds).
- A specialized fetch path (e.g. a paid rendering proxy like Scrape.do) whose
  own internal HTTP client has a *longer* timeout because headless-Chrome
  rendering through a residential proxy genuinely takes longer (60-90s) to
  return a *successful* response.

If the generic short timeout wraps the specialized path via
`asyncio.wait_for(specialized_call(), timeout=generic_short_timeout)`, every
call gets cut off before the specialized path's own longer-but-legitimate
response can land. The result isn't "slow" — it's a total, silent failure:
0 successful fetches on every attempt, for every URL, indistinguishable from
"discovery doesn't run at all" (no discovered links, no explicit error,
because each fetch fails via the *generic* TimeoutError path, not the
specialized client's own exception).

This is a different failure mode from a budget-exhaustion bug (see
`discovery-budget-guard.md`): even after fixing the outer-deadline budget
math, a mismatched *inner* per-call timeout can still make every single call
fail before the deadline math ever matters.

## The fix pattern

- Detect which fetch path is active from the per-uni config flag that selects
  it (e.g. `discovery_config.scrape_do_skip_fallbacks`), and widen the
  `asyncio.wait_for` timeout to match-or-exceed that path's own internal
  client timeout (with a small margin), instead of using the generic
  short default.
- Retries that don't change the input (same URL, same timeout) are close to
  useless once a genuine timeout has occurred on a slow-by-design path —
  skip the discovery-level "retry once" logic for that path rather than
  doubling an already-long wait.
- Independent seed/listing URLs at the same BFS depth have no ordering
  dependency on each other. When each individual fetch is expensive (as with
  a rendering proxy), fetch them concurrently (`asyncio.gather`) instead of
  sequentially through the BFS queue — this turns "N slow calls summed" into
  "N slow calls in parallel," which is often the difference between fitting
  inside an outer phase deadline and blowing through it.

## How this was found

Even after fixing the *outer* discovery-phase budget-exhaustion bug
(`discovery-budget-guard.md`), the same university was still failing every
run at exactly the outer deadline. The tell: check whether the failing
university's config routes fetches through a specialized slow-but-successful
path, and compare that path's *own* internal client timeout against whatever
generic timeout wraps the call site — a mismatch there produces 100% fetch
failure independent of the outer budget math being correct.
