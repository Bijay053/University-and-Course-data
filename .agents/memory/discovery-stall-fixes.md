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
