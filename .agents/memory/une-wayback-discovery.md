---
name: UNE Wayback CDX discovery
description: University of New England (uni_id=2288) course discovery via Wayback CDX — why BFS/sitemap fail and how CDX is configured.
---

# UNE Course Discovery

**Why BFS fails**: `/study/study-options/courses` is a React SPA. Courses are loaded via XHR JSON, not injected as `<a>` tags. Even Scrape.do render=true returns 0 course links.

**Why sitemap fails**: UNE has no sitemap.xml.

**Why Wayback CDX is the only path**: CDX archives past crawls of the live site, including `/study/courses/{year}/{slug}` URLs. These are the canonical course detail pages.

**Config**: `une_2288.yaml`
- `bfs_page_budget: 0` — skip BFS entirely (saves the full 300s budget for CDX)
- `use_wayback: true` — supplemental mode (CDX runs regardless of BFS results)
- `scrape_do_skip_fallbacks: true`, `scrape_do_render: true` — CF Enterprise bypass
- No year-based block_url_patterns — CDX returns multi-year slugs; year-dedup handles it

**CDX prefix**: Added `www.une.edu.au → www.une.edu.au/study/courses/*` to `_HOST_CDX_URL_PREFIX` in `wayback_discover.py`. Without this, the default `www.une.edu.au/*` query hits 10k SURT-ordered non-course URLs before reaching the course subtree.

**Extraction**: `single_course.py:1337` auto-appends `?international=true` to all UNE `/study/courses/` URLs.

**Why**: Confirmed by two completed jobs (imported=0) + Celery log showing 0 course links extracted from the rendered listing page.
