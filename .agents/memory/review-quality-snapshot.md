---
name: Review quality snapshot
description: Keep live review quality scores bound to the exact staged row snapshot displayed to reviewers.
---

Review quality scores and chips must be computed from the same loaded row snapshots as the review table, not from a second university-scoped read.

**Why:** During a running scrape, the staged row can gain a degree level between independent requests. A score calculated before that update can show “No Degree Level” next to a displayed Master level, even when the quality checker itself is correct. The same race applies to reviewer edits.

**How to apply:** Add future quality signals to the staged review projection, or carry a verifiable row revision across requests; avoid reintroducing an unfenced independent quality fetch for the review table.