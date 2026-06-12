---
name: Lancaster SSR Vue prop discovery
description: Lancaster listing pages look JS-rendered but :courses-data prop is server-side embedded JSON — plain httpx gets all 538 courses.
---

# Lancaster University listing-page discovery

## The rule
Lancaster's `/study/undergraduate/courses/` and `/study/postgraduate/postgraduate-courses/` listing pages use a Vue 3 `<course-listing>` component. While the rendered course list requires JS, the **`:courses-data` prop is server-rendered directly into the HTML** as single-quoted JSON with HTML-escaped characters (use `html.unescape()` before `json.loads()`).

**Why:** The Vue component receives all data via SSR props, not XHR; the server-side template embeds the full JSON array in the attribute. Confirmed 2026-06-12 by direct httpx fetch + regex extraction.

**How to apply:** `lancaster_listing.py` fetches both listing pages, applies `re.search(r":courses-data='([^']{10,})'")`, unescapes, parses. Returns `{"name": ..., "url": ...}` dicts. Activated by `discovery.lancaster_listing: true` in YAML; wired into `orchestrator.py` after the SearchStax block.

## Entry year format
The `entryYear` field is `"26/27"` (not `"26"` or `2026`). Filter: `str(c.get("entryYear","")).startswith("26/")` for 2026.

## Course counts (2026 entry)
- UG: 371 (838 total records across all years)
- PG: 167 (397 total records)
- Total: 538 course URLs, all HTTP 200

## URL construction
- UG: `https://www.lancaster.ac.uk/study/undergraduate/courses/{slug}/2026/`
- PG: `https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/{slug}/2026/`

Individual course detail pages are fully SSR — plain httpx works fine. No Playwright needed anywhere in the Lancaster pipeline.

## Files touched
- `backend-py/app/services/scraper/lancaster_listing.py` — provider
- `backend-py/app/services/scraper/config/schema.py` — `lancaster_listing` + `lancaster_listing_year` fields on `DiscoveryConfig`
- `backend-py/app/services/scraper/orchestrator.py` — wired after SearchStax block (~line 932)
- `backend-py/scraper_config/unis/lancaster.yaml` — `discovery.lancaster_listing: true`
