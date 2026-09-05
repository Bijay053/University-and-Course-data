---
name: Snapshot canary cleanup and locking
description: Safety rules for recurring object-storage canaries in a pooled multi-worker service.
---

Storage canaries must delete the exact object version returned by the write, verify the key is absent, and retain a short lifecycle rule for interrupted cleanup.

**Why:** A normal delete in a versioned bucket creates a delete marker instead of permanently removing test data. A failed or cancelled cleanup can otherwise leave untracked canary objects indefinitely.

**How to apply:** Use unique keys, exact `VersionId` deletion when returned, an absence check, and a short current/noncurrent lifecycle expiry for the canary prefix.

Fleet-wide monitor exclusion must use a transaction-scoped PostgreSQL advisory lock.

**Why:** Session-scoped advisory locks can remain attached to a pooled connection after the ORM commits and releases it, silently suppressing every future canary.

**How to apply:** Acquire `pg_try_advisory_xact_lock` in the transaction that records the check and let commit or rollback release it automatically.