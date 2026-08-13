---
name: Notre Dame force_wayback_first extraction
description: Why Notre Dame scrapes took 2-3 hours and the fix — force_wayback_first flag skips all scrape.do per course.
---

## Root cause of 2–3 hour Notre Dame scrapes

`discovery.scrape_do_skip_fallbacks: true` fires for BOTH discovery AND extraction
(it's a host-level check in fetch_html, not scoped to discovery only).

For each of ~700 Notre Dame course pages during extraction:
1. scrape.do static → 57 s ROTATION_FAILED (fast-fail, but scrape.do still takes 57 s to respond)
2. scrape.do render=True → 23 s → HTTP 200, real course HTML ✓

57 s + 23 s = 80 s/course × 700 courses ÷ 5 concurrency = **2.7 hours**.

## The fix: `extraction.force_wayback_first: true`

New field added to `ExtractionConfig` (schema.py) and wired into `fetch_html`
(http_fetcher.py, before the discovery fast-path at the `not _scrape_do_render and
not _scrape_do_static` block). When True, tries Wayback Machine BEFORE any live
scrape.do attempt. Falls through to live path if Wayback returns None.

**Why Wayback is fast here:** `wayback_discover()` pre-loads CDX timestamps into
`_wayback_ts_cache` during discovery. `fetch_html_wayback` hits the CDX-cached fast
path: direct `https://web.archive.org/web/{ts}id_/{url}` fetch = ~1.4 s/course.

**Result:** 700 × 1.4 s ÷ 5 concurrency ≈ **3 minutes** (vs 2.7 hours).

## discovery.scrape_do_render: true also needed

The sitemap fetch during discovery was ALSO using the 57 s static (ROTATION_FAILED).
`discovery.scrape_do_render: true` (field on DiscoveryConfig, wired at
http_fetcher.py:990) skips the static call and goes straight to render=True.

Sitemap via render=True: 9.6 s, 474 program URLs. Campus URLs (not in sitemap)
still come from Wayback CDX (38 s, keep `use_wayback: true`).

## Tested values (Aug 2026)
- scrape.do static on notredame.edu.au → 57 s, 502 ROTATION_FAILED
- scrape.do render=True on sitemap → 9.6 s, 1.5 MB, 474 /programs/ URLs
- scrape.do render=True on course page → 23 s, 272 KB, real course HTML
- Wayback CDX-cached fetch → 1.4 s, 74 KB archived HTML

**Why:**
Any CF-Enterprise host where static ALWAYS gets ROTATION_FAILED AND
`discovery.scrape_do_skip_fallbacks=True` is set → each course extraction burns
80 s on failed scrape.do before falling through. force_wayback_first skips this.
