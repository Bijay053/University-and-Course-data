---
name: Bond University sitemap discovery (DEPRECATED)
description: Bond sitemap child pages now 403; use ES API instead. Kept for historical reference.
---

## Status: DEPRECATED — use ES API

As of August 2026, `bond.edu.au/sitemap.xml?page=N` returns HTTP 403 from datacenter IPs for ALL child pages.
The sitemap index root resolves but its 13 child pages all 403, so the supplement produces 0 additional URLs.

See `bond-es-discovery.md` for the current working approach (generic_search_api → ES endpoint).

## Historical note (pre-August 2026)

The sitemap at `https://bond.edu.au/sitemap.xml` was a sitemap index with 5 child pages.
Pages 1–4 had 247 total program+microcredential URLs (240 depth-2 base course pages).
`always_sitemap_supplement: true` ensured it ran even after BFS found the 13 homepage courses.

URL patterns:
- Keep: `/program/{slug}$` and `/microcredential/{slug}$` (depth-2 only)
- Block: `/program/{slug}/{sub}` depth-3+ sub-pages
