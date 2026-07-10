---
name: ECU discovery (Funnelback + PG seeds)
description: How ECU course discovery works — Funnelback BFS pagination for UG, explicit seeds for PG, doubled-param timeout bug and fixes.
---

## Rule

ECU has two Cloudflare/SPA listing pages that produce 0 course links even with Scrape.do render=true:
- `/degrees/undergraduate` — React SPA, Funnelback-rendered
- `/degrees/postgraduate` — React SPA, Funnelback CF-blocked

**UG discovery**: Pre-seed ALL 12 Funnelback pagination pages explicitly in `seed_urls`
(start_rank=11 through start_rank=111 — 10 courses per page). BFS must NOT follow
`?start_rank=` links between pages. Reason: Funnelback's JS appends the next page's
params to the current URL's query string instead of replacing them, producing doubled
params (?collection=X&collection=X&waitFor=3000&waitFor=3000). The doubled URL passes
BOTH BFS dedup checks (different exact string AND different normalized form from the
clean pre-seeded URL), enters the BFS queue, and hangs 80s+ on Scrape.do render=true
— blowing the 300s discovery budget.

**PG discovery**: No listing page works. Use explicit seed URLs — one confirmed PG course
page per faculty. Cross-links from each seed yield additional PG candidates. (~15 seeds
after removing stale 404s as of 2026-07-10.)

## The doubled-param bug (2026-07-10, fixed generically in discovery.py)

Two discovery.py fixes prevent this for ALL universities:

1. `_resolve()` now rejects any URL where any query-param key appears more than once
   (`parse_qsl` key-set size check). Doubled-param URLs are always a URL-generation bug.

2. `block_url_patterns` is now a BFS traversal-level guard (compiled early, checked
   before each fetch) in addition to the existing post-BFS candidate filter. For
   Scrape.do-backed discovery this saves ~6s per blocked URL — critical for ECU which
   previously burned ~15 extra BFS slots on /future-students/*, /study/events, and
   /degrees/study-areas/* pages discovered as nav links.

## What NOT to do

- `always_sitemap_supplement: true` — ECU sitemap.xml is CF-blocked (0 bytes).
  6 probes × ~25s each = ~150s wasted → 300s timeout. Never set for ECU.
- `start_rank=` in `allow_url_patterns` — causes BFS to follow pagination links between
  pages; the doubled-param URLs generated from those links hang Scrape.do.
- Broad `allow_url_patterns: [/degrees/courses/]` — matches Funnelback tab URLs
  (?f.Tabs=Staff, ?f.Tabs=AskUs) that waste BFS slots with 0 new candidates.

## allow_url_patterns (correct)

```yaml
allow_url_patterns:
  - /degrees/courses/[a-z]+-   # course detail slugs (contain at least one hyphen)
  # start_rank= intentionally NOT here — all pagination pages pre-seeded instead
```

## Verified results (dev, 2026-07-10)

Found:153 | Staged:79 | Skipped:74 (52 domestic-only, 15 online-only, 7 category-landing)
FetchFailed:0 | Errors:0 | Discovery:79s | bfs_page_budget=60

**Why discovery=79s:** block_url_patterns traversal guard skips ~17 non-course nav pages
before fetching them; _resolve() duplicate-param guard stops doubled Funnelback URLs
from entering the queue entirely.
