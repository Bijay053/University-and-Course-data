---
name: UC Canberra SPA + hidden-input fees
description: University of Canberra discovery fix (sitemap) and fee extraction from hidden inputs in static HTML.
---

## Rule
UC course listing pages are 4-byte SPA shells. Use the XML sitemap for discovery. Extract fees from hidden inputs already present in the static HTML.

**Why:** BFS, Wayback CDX, and browser all return 0 course links — the listing page is a client-side SPA. The sitemap at `/services/wcm/site-map/course.xml` lists all 366 course pages (184 unique after year-dedup). Fees are embedded as hidden `<input>` elements in the static httpx response.

**How to apply:**
- `canberra_2239.yaml`: `sitemap_url`, `allow_url_patterns: ['/course/']`, `year_dedup_mode: keep_preferred_year`, `year_dedup_preferred_year: 2027`
- Fee extraction: `_from_uc_hidden_inputs()` in `fee.py` — reads `id="N-year"` / `id="N-eftsl-international"` input pairs, filters by `id="current-year"` value (fallback: latest year ≤ current-year).
- Location: "Enrolments" (a statistics section header, not a campus) was appearing as location — fixed by adding `enrolments?` to `_NAV_TEXT_LOCATION_RE` in `location.py`.

## Discovery URL cache trap
When running test scrapes with `maxCourses`, the 7-day discovery URL cache stores only the small test set. Always use `forceDiscovery: true` for the real production run after testing, or the cache returns the limited test URLs.

## Outcome
Found:184 | Staged:172 | Fee fill: 87.8% (21 courses have empty eftsl-international = domestic-only, no intl fee published) | Avg completeness: 83%.
