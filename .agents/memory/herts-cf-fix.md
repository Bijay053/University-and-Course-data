---
name: Herts CF Enterprise fix
description: University of Hertfordshire (herts_2226.yaml) — Cloudflare Enterprise block, sitemap-based discovery via Scrape.do render=true.
---

## Rule

www.herts.ac.uk has the same Cloudflare Enterprise block as Cardiff (2194) and Kingston (2193):
- plain httpx/cffi → 403 on ALL paths
- Scrape.do static → 502 ROTATION_FAILED
- Scrape.do render=true → 200

Fix: `scrape_do_skip_fallbacks: true` in both discovery and extraction.
http_fetcher.py auto-escalates static 502 → render=true automatically.

## Discovery

Listing pages (`/courses/undergraduate` etc.) are lazy-AJAX SPAs — render=true
yields only 27 nav-hub links. BFS is useless.

**Sitemap** is the reliable source:
- `sitemap.xml` → 3.9 MB via render=true, 844 course paths
- NOT paginated: `sitemap.xml` and `sitemap.xml?page=1` return identical content
- Point `sitemap_url` directly to `https://www.herts.ac.uk/sitemap.xml`

Config: `bfs_page_budget: 1`, `always_sitemap_supplement: true`, `use_wayback: false`

## Sitemap URL categories

| Pattern | Count | Action |
|---------|-------|--------|
| /courses/undergraduate/ | 142 | ✓ KEEP |
| /courses/postgraduate-masters/ | 281 | ✓ KEEP |
| /courses/research/ | 131 | ✓ KEEP |
| /courses/foundation/ | 5 | ✓ KEEP |
| /courses/online-distance-learning/ | 12 | ✓ KEEP |
| /courses/short/ | 143 | ✗ block |
| /courses/undergraduate-courses/ | 64 | ✗ block (old hub pages) |
| /courses/postgraduate-masters-study/ | 24 | ✗ block (old hub pages) |
| /courses/degree-apprenticeships/ | 24 | ✗ block (UK-only) |
| others | ~18 | ✗ block |

Net after filtering: ~571 course candidates.

**Why:** `/courses/undergraduate-courses/` looks like courses but are 2+ segment deep category hub pages (e.g. `/courses/undergraduate-courses/art-design-and-fashion/applicant-page`). `/courses/postgraduate-masters-study/` similarly has hub paths like `/postgraduate-masters-study/computer-science-ai-robotics`.
