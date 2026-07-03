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
