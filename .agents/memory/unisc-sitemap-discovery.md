---
name: UniSC (University of the Sunshine Coast) discovery
description: UniSC degree finder is client-side AngularJS filtering; all course URLs come from XMLsitemap, not BFS or an API.
---

## Rule
Use `discovery.sitemap_url: https://www.unisc.edu.au/XMLsitemap` with `always_sitemap_supplement: true` and depth-2-only URL filtering.

**Why:** The "Find a degree" page (unisc.edu.au/programs) uses AngularJS `ProgramFinderCtrl` which filters course cards client-side using `p.cricosCode` for international. No external JSON API is exposed. BFS only finds ~76 of 134 international courses because category hub pages link to sub-categories, not all course pages directly.

## Sitemap structure (XMLsitemap, 15MB)
- Depth-1 = category hubs (34 pages) — never course candidates
- Depth-2 = course overview pages (391 total) — the scraping targets
  - bachelor-degrees-undergraduate-programs: 120
  - postgraduate-degrees: 72
  - majors-and-minors: 117 (BLOCK — sub-components of degrees, not standalone)
  - previous-student-handbooks: 24 (BLOCK — handbooks)
  - headstart: 8 (BLOCK — bridging)
  - short-courses-and-microcredentials: 4 (BLOCK)
  - tertiary-preparation-pathway: 5 (BLOCK)
  - small subject-area sub-groups: ~31 (keep — real programs)
- Depth-3+ = intake-specific pages (1027+) — BLOCK

## Domain
- `www.usc.edu.au` redirects to `www.unisc.edu.au` (rebranded) — DB scrape_url already uses new domain.

## How to apply
If the category list changes, re-fetch the sitemap and re-run the depth analysis. The `force_candidate_url_patterns` regex lists valid depth-1 category slugs — update if UniSC adds new degree categories.
