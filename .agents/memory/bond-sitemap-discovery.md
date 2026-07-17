---
name: Bond University discovery
description: Bond program-finder is a React SPA; sitemap index is the only reliable discovery source.
---

## Rule
Use `discovery.sitemap_url: https://bond.edu.au/sitemap.xml` with `always_sitemap_supplement: true`. Do NOT rely on `generic_search_api` for Bond.

**Why:** Bond's homepage exposes ~13 featured course cards in static HTML. The orchestrator's `generic_search_api` handler is gated by `if not links` (orchestrator.py line ~1710) — BFS fills `links` first so the API is never called. The Elasticsearch endpoint (`POST bond.edu.au/api/v1/elasticsearch/bond_prod_default/_search`) itself works and returns 220 URLs, but the orchestrator condition prevents it from running.

**How to apply:** The sitemap at `https://bond.edu.au/sitemap.xml` is a sitemap index listing 5 child pages. Sitemap discovery recurses one level into index files. Pages 1–4 have 247 total program+microcredential URLs (240 are depth-2 base course pages). `always_sitemap_supplement: true` ensures it runs even after BFS finds the 13 homepage courses.

## URL patterns
- Keep: `/program/{slug}$` and `/microcredential/{slug}$` (depth-2 only)
- Block: `/program/{slug}/{sub}` depth-3+ (7 sub-pages: FAQ, prerequisites, enquiry, entry-assessment)

## If generic_search_api is ever needed for other unis
The `if not links` gate means any university with even a few BFS-visible course links will skip the YAML API. Use `sitemap_url` + `always_sitemap_supplement` for those cases instead, OR set `seed_urls` to empty so BFS finds 0 links before the API runs.
