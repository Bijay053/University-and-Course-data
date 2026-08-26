---
name: Targeted continuation review scope
description: How review sets should behave when unresolved URLs are continued in one or more targeted child jobs.
---

Treat a completed scrape and its explicit unresolved-continuation children as one review set. Starting from the latest job, walk backward through `retrySourceJobId` and include pending staged rows from every job in that bounded, cycle-safe chain.

**Why:** Each continuation gets a new runtime job ID. Filtering review rows only by the latest ID makes a 120-course original plus a 50-course continuation appear as only 50 courses, hiding the original successful batch.

**How to apply:** Aggregate only explicit parent links and require the same university. Do not broaden ordinary standalone reviews to all historical pending rows for that university.