---
name: Skip reason counter conservation
description: Accounting invariant for completed scrape skip totals and diagnostic reason buckets.
---

Every increment of a completed scrape's skipped-course total must go through the shared accounting boundary that increments exactly one normalized reason bucket.

**Why:** Non-staging exits such as extraction-time policy drops, deduplication, and recovery replay rejection can bypass the normal staging result flow. Independent counter mutations made completed totals disagree with operator diagnostics.

**How to apply:** When adding any intentional course exclusion to bounded, full, or recovery scrape paths, record it through the shared skip helper. Use errors or fetch-failure counters instead when the course was not intentionally excluded.