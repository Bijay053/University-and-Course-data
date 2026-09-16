---
name: Required-fee resume checkpoints
description: Safety rule for interrupted scrapes whose recipes require exact authoritative central-fee matches.
---

Pending rows from an interrupted scrape cannot prove which central fee source produced their values. A recipe that requires an exact central-fee match must reprocess those URLs instead of counting the pending rows as completed resume checkpoints.

**Why:** A resumed SIT scrape inherited fee-less pending rows from an earlier attempt. The new extraction path correctly failed closed for newly processed courses, but the inherited rows still appeared in the new review chain.

**How to apply:** Intersect all prior pending review URLs—including rows owned by completed jobs—with the current discovered work list, reprocess those URLs, and retain their exact row IDs. Remove only those still-pending IDs after the replacement job finishes cleanly; preserve them if the replacement stops, fails, degrades, or completes with warnings.