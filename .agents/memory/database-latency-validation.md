---
name: Database latency validation contention
description: Interpreting PostgreSQL timing gates during parallel validation
---

Check an isolated rerun before treating a parallel database timing failure as
an application regression; do not loosen the budget merely to make it pass.

**Why:** A catalogue-history read exceeded its two-second ceiling during
parallel validation with rollback-owned migration/trigger fixtures, while
passing independently. Rollback isolation prevents persisted data changes but
does not eliminate concurrent database load or DDL locking.

**How to apply:** Keep query-count and byte assertions intact, inspect competing
database fixtures, and distinguish isolated timing evidence from parallel
validation results. Prefer isolated schemas for future heavy benchmark fixtures.