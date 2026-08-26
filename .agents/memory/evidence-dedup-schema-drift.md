---
name: Evidence dedup schema drift
description: Runtime database constraints can lag SQLAlchemy metadata for scraped-field evidence.
---

Do not assume the model-declared uniqueness constraint for scraped-field evidence exists in a long-lived development database. Verify the live PostgreSQL indexes/constraints before using a named `ON CONFLICT` target in maintenance scripts.

**Why:** A global backfill found that the ORM model declared an evidence dedup constraint while the live database had only the primary-key index. Named conflict handling therefore failed at statement preparation and rolled back the transaction.

**How to apply:** For large maintenance writes, inspect `pg_indexes`/`pg_constraint` first. If the dedup constraint is absent, serialize script instances with a transaction advisory lock and use compare-and-set, blank-only writes so evidence remains idempotent without relying on a missing constraint.