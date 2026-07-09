---
name: ECU discovery (Funnelback + PG seeds)
description: How ECU course discovery works — Funnelback BFS pagination for UG, explicit seeds for PG, what NOT to do.
---

## Rule

ECU has two Cloudflare/SPA listing pages that produce 0 course links even with Scrape.do render=true:
- `/degrees/undergraduate` — React SPA, Funnelback-rendered
- `/degrees/postgraduate` — React SPA, Funnelback CF-blocked

**UG discovery**: BFS starting at `/degrees/courses/all`. The rendered page shows
10 courses and nav links include `?start_rank=11/21/...` pagination links. Each
pagination page adds 10 unique UG course slugs. ~10 Funnelback pages covers all
~100 UG courses.

**PG discovery**: No listing page works. Use explicit seed URLs — one confirmed
PG course page per faculty. Cross-links from each seed yield additional PG candidates.
Seeds confirmed 2026-07-09: master-of-business-administration, master-of-nursing,
master-of-engineering-science, master-of-education.

## What NOT to do

- `always_sitemap_supplement: true` — ECU sitemap.xml is Cloudflare-blocked (0 bytes).
  discover_from_sitemap() probes 6 sitemap URL candidates × ~25s each = ~150s wasted,
  triggering the 300s discovery timeout. **Never set this for ECU.**
- Broad `allow_url_patterns: [/degrees/courses/]` — overrides global block for
  Funnelback tab URLs (`f.Tabs=Staff`, `f.Tabs=AskUs`, `f.Tabs=AllDocumentsFill`)
  that waste ~5 BFS page slots with 0 new candidates each.

## allow_url_patterns (correct)

```yaml
allow_url_patterns:
  - /degrees/courses/[a-z]+-   # course detail slugs (contain at least one hyphen)
  - start_rank=                 # Funnelback pagination only
```

This allows course detail pages and pagination but prevents tab variant URLs from
overriding global blocks during BFS traversal.

## block_url_patterns (post-filter, applied to candidate set)

```yaml
block_url_patterns:
  - start_rank=       # prevents pagination page URL itself being a candidate
  - collection=ecu%7E
  - f\.Tabs%7C
```

Note: block_url_patterns is a post-BFS filter on candidates — it does NOT prevent
BFS from visiting these URLs. That's what allow_url_patterns controls.

## Expected results

With bfs_page_budget=25, no sitemap probing, specific allow_url_patterns:
- Discovery: ~125-237s (well within 300s budget)
- UG courses: ~35-103 Bachelor's (depends on Funnelback pagination depth reached)
- PG courses: ~5 (2 Doctorates + 1 Grad Cert + 2 Master's from seeds)

**Why:** ECU's post-filter in discovery.py lines 1789-1823 restricts final candidates
to only `/degrees/courses/<single-slug>` patterns. The allow/block pattern split
ensures BFS reaches the right pages without wasting budget on useless nav variants.
