---
name: Discovery-phase stall prevention
description: How to stop a scrape job's discovery phase from hanging silently (stuck HTTP fallback chain) and how per-uni sitemap config must be wired.
---

## scrape_do_skip_fallbacks must be set under BOTH `extraction:` and `discovery:`

They are two independent flags read at different call sites in
`http_fetcher.py` (`get_uni_config().extraction.scrape_do_skip_fallbacks` vs
`get_uni_config().discovery.scrape_do_skip_fallbacks`). Setting only the
`extraction:` one leaves every discovery-phase fetch (sitemap probe, robots.txt,
BFS page fetch) walking the full httpx → curl_cffi → Wayback → Scrape.do
fallback chain even on a Cloudflare-Enterprise host where every leg but the
last is guaranteed to fail/block.

**Why:** discovered via a real stall — a uni with the extraction flag set but
not the discovery flag hung for minutes on the discovery-phase sitemap fetch
alone, because that fetch never got the fast-path.

**How to apply:** whenever a per-uni YAML sets `scrape_do_skip_fallbacks` for
a Cloudflare/bot-protected host, set it under BOTH sections unless discovery
genuinely needs to try cheaper transports first.

## Explicit per-uni `sitemap_url` should skip generic probing entirely

`discover_from_sitemap` in `sitemap.py`: when a university has an explicit
`discovery.sitemap_url` configured, fetch ONLY that URL. Do not also probe
the 4 generic sitemap-index paths + robots.txt "just in case" — on a slow or
partially-blocked host, mixing a guaranteed-good explicit URL in with 4 extra
speculative fetches turns one attributable slow fetch into an unattributable
multi-minute silence (you can't tell from the logs which of the 5 candidates
is actually stuck).

**How to apply:** gate the candidate list construction on `if sitemap_url: candidates = [sitemap_url]` vs. the full probe-list fallback, and log the two
paths differently (e.g. "fetching configured URL" vs "probing N URL(s)") so
the discovery log itself tells you which path was taken.

## A YAML comment describing a config value is not the same as setting it

A per-uni YAML had a detailed comment block saying "Explicit sitemap URL —
fetched via Scrape.do..." directly above the `discovery:` section, but the
actual `sitemap_url:` key was never added — it silently resolved to `None`
at runtime, so a fully-correct short-circuit implementation still fell
through to the slow generic-probing branch. From the logs alone this looked
identical to "stale code hasn't deployed", wasting a full debug cycle.

**Why:** comments describe intent, not runtime state; a key that's
documented-but-missing produces zero errors or warnings — the config loader
has no way to know the omission was a mistake vs. intentional.

**How to apply:** when a stall/regression report says "the fix isn't taking
effect" after a code change shipped, check resolved config values before
suspecting stale workers or code-path mismatches — add (or look for) a log
line that prints the actual resolved value of the relevant config field at
the top of the function, and diff it against what the YAML *comment* claims.
Also add a test that loads the YAML and asserts the key is actually set
(not just present in a docstring/comment) for any config value a fix
depends on.

## Gate expensive fallback probes behind the same "explicit config wins" rule

If a university has an explicit `sitemap_url` configured, an unrelated
"alternative listing paths" probing tier (guessing at generic paths like
`/our-courses`, `/all-courses`) should be skipped entirely too — not just
made conditional on candidate count. An operator-supplied explicit source
means further guessing on the same (likely blocked) host is waste, even if
the primary explicit-source fetch came up short for some other reason.

**How to apply:** any "guess a well-known fallback path" probing tier should
check for an explicit per-uni override of the same kind before running, not
just a generic "found < threshold" condition.

## Discovery-phase deadline pattern

Wrap the whole `discover_course_links(...)` call (BFS + sitemap fallback +
sitemap supplement all happen inside it) in `asyncio.wait_for(timeout=...)`
at the orchestrator call site, not inside individual fetch helpers. On
`asyncio.TimeoutError`, raise a plain exception with a clear message and let
it propagate — the existing outer `except Exception` handler in the Celery
task already calls `_mark_failed()`, so no new claim-release code is needed.
Per-probe timeouts (e.g. `asyncio.wait_for` around a single `fetch_html`
call) are still worth adding separately as a first line of defense, but the
top-level deadline is the backstop that guarantees the job can never hold a
worker slot indefinitely regardless of which inner call hangs.

## "N unique candidates" logs need a reason branch, not just a count

A sitemap/discovery step that only ever logs the final candidate count (e.g.
"done — 0 unique candidates") is indistinguishable across three completely
different root causes: the fetch itself failed (0 bytes / non-XML), the fetch
succeeded but the document had no extractable entries, or entries were found
but every one got filtered out by URL patterns. Each needs a different fix.

**How to apply:** any discovery/collection loop that can legitimately end at
zero should emit a distinct diagnostic event per failure branch (fetch
failure vs. empty-parse vs. all-filtered), and the all-filtered branch should
include a small URL sample so a human can immediately see *which* filter is
too aggressive without re-running the job with extra logging.

## Search/discovery redirector links must be unwrapped before URL filtering

Some university search/CMS platforms (e.g. Funnelback: `/s/redirect?...&url=<encoded-target>`)
return real course links wrapped in a redirector query string. If the
allow/block URL-pattern filters run on the *wrapped* URL, every real link is
judged against a `/s/redirect` path instead of its actual destination and
gets rejected — this can look identical to "the site has no course links" in
the logs. The same unwrap must be applied in every place a raw link is
observed: server-side link resolution, the orchestrator's listing-page
render/filter pass, and the browser-side link-extraction JS — missing any
one of the three reintroduces the bug for that code path only, which is easy
to miss since the other two paths look fixed.

**How to apply:** write one small `unwrap_x_redirect()` helper (parse the
query string, `unquote` the target param) and call it at the earliest point
each code path first sees a raw href, before any pattern/path filtering runs
against it. Pin the browser-side JS source unwrap logic with a dedicated
test so a future edit to `_EXTRACT_LINKS_JS` can't silently drop it.
