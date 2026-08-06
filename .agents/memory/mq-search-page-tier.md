---
name: MQ search-page discovery tier
description: New Tier 1.5 in mq_browser_discover.py — stealth-browser pagination of /search?query=&category=courses captures research degrees missing from coursehandbook
---

## Rule

MQ discovery has THREE tiers now (as of August 2026):
1. **Coursehandbook sitemap** — resolves ~223 UG/PG courses via httpx title extraction
2. **Search-page harvest** (NEW) — stealth browser paginates `https://www.mq.edu.au/search?query=&category=courses` with `start_rank` + `num_ranks=100` URL params; captures research degrees at `/study/find-a-course/research/<slug>` that the handbook never emits
3. **Browser sweep** — retained as fallback; contributes 0 in CF-blocked dev

**Why:** The coursehandbook sitemap + resolver only gave ~127–223 courses. MQ's search page (Squiz Matrix / Funnelback backend) indexes all 367 international courses including Doctor of Philosophy and Professional Doctorates whose admissions URLs are at `/study/find-a-course/research/<slug>` — a path the handbook resolver never constructs (it always uses `/courses/<slug>`).

**How to apply:** If a future MQ scrape returns < 300 courses, check which tiers fired:
- If search-page returns 0: stealth browser probably hitting CF in dev (expected in Replit sandbox — works in production)
- If handbook returns < 150: check if coursehandbook.mq.edu.au sitemap structure changed
- If search-page stalls at one page: `start_rank` param might be ignored → click-based fallback in `_discover_from_search_page` kicks in

## Key code facts

- `_SEARCH_COURSE_LINK_RE` matches `/study/find-a-course/{undergraduate|postgraduate|research|courses}/<slug>`
- `_COURSE_PATH_RE` updated to include `research` and `courses` tokens (was only `undergraduate|postgraduate`)
- `_DISCOVERY_FLOOR` raised from 150 → 300
- `browser_discover_mq()` default `max_courses` raised from 300 → 500
- Search-page tier uses same stealth_context() as handbook sitemap tier

## URL shape note

Research degree admissions URLs: `/study/find-a-course/research/doctor-of-philosophy`
These are BLOCKED as listing root in mq.yaml ONLY with `/?$` suffix — detail pages (with a slug) are NOT blocked.
