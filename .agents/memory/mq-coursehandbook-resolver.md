---
name: MQ coursehandbook resolver fix
description: Macquarie University discovery — httpx vs patchright for the two-phase resolver pattern
---

## Rule

MQ uses a two-phase discovery approach in `mq_browser_discover.py`:
1. **Sitemap fetch** (`_discover_from_coursehandbook_sitemap`): must use **patchright** (stealth_context). httpx gets HTTP 403 in Celery context (CF protects the coursehandbook sitemap XML endpoint).
2. **URL resolver** (`_resolve_to_study_urls`): must use **httpx.AsyncClient** (20 concurrent, 20s timeout). patchright was the old approach — it timed out at 15s per page, resolving only 127/383 handbook entries. httpx resolves 223/383 in ~11s total.

## Result

- Before fix: 127 admissions URLs resolved
- After fix: 223/383 unique admissions URLs resolved (~75% improvement)
- BFS supplement seeds (`_FACULTY_SEED_URLS`) all return 0 course anchors in Replit dev (CF blocks them); they contribute in production only.
- The 143-course gap vs the 366 advertised total is research/honours/combined degrees not in the coursehandbook — only discoverable via BFS browser sweep in production.

**Why:** The coursehandbook sitemap *index page* requires CF-bypass (patchright), but individual course detail pages on coursehandbook.mq.edu.au return their HTML title in plain httpx with a 200 — CF only guards the sitemap index, not the course pages themselves.

**How to apply:** Any time MQ discovery shows < 200 courses resolved from handbook, check which phase is failing. If the sitemap harvests < 100 entries → patchright broken. If resolver gives < 200/383 → httpx broken (switch back to patchright). BFS contributing 0 in dev is always expected.
