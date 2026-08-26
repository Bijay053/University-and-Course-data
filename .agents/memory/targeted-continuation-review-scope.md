---
name: Targeted continuation review scope
description: How review sets should behave when unresolved URLs are continued in one or more targeted child jobs.
---

Treat a completed scrape and its explicit unresolved-continuation children as one review set. Starting from the latest job, walk backward through `retrySourceJobId` and include pending staged rows from every job in that bounded, cycle-safe chain. Targeted continuations must also skip ordinary stale-review replacement cleanup so the parent rows still exist to aggregate.

**Why:** Each continuation gets a new runtime job ID. Filtering review rows only by the latest ID hides the original successful batch; running replacement cleanup at continuation startup can delete that batch entirely.

**How to apply:** Aggregate only explicit parent links and require the same university. Detect explicit course-URL retries before cleanup and preserve their source review rows. Do not broaden ordinary standalone reviews to all historical pending rows for that university.