---
name: Targeted continuation review scope
description: How review sets should behave when unresolved URLs are continued in one or more targeted child jobs.
---

Treat a completed scrape and its explicit unresolved-continuation children as one review set. Starting from the latest job, walk backward through `retrySourceJobId` and include pending staged rows from every job in that bounded, cycle-safe chain. Targeted continuations must also skip ordinary stale-review replacement cleanup so the parent rows still exist to aggregate.

**Why:** Each continuation gets a new runtime job ID. Filtering review rows only by the latest ID hides the original successful batch; running replacement cleanup at continuation startup can delete that batch entirely.

**How to apply:** Aggregate only explicit parent links and require the same university. Detect explicit course-URL retries before cleanup and preserve their source review rows. Do not broaden ordinary standalone reviews to all historical pending rows for that university.

Pre-stage extraction snapshots are only a legacy, partial recovery source. Exact review restoration must use a durable backup captured from the final staged row after normalization, inheritance, scoring, overrides, and verification.

**Why:** The extractor payload can differ materially from the persisted review row because staging transforms and filters fields. Replaying it cannot reconstruct the historical operator review set exactly.

**How to apply:** Save the final staged values atomically with the review row, refresh the backup after any later verification update, prefer exact backups over legacy snapshots, and restore under the same lock used by approval.