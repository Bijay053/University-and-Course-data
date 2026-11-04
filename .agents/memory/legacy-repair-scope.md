---
name: Legacy database repair scope
description: Connection hardening must not expand the work authorized by a targeted repair.
---

Keep still-supported legacy repair entrypoints narrowly scoped; securing their
connections must not silently turn them into a full Alembic upgrade.

**Why:** The user explicitly required preserving their migration semantics and
prohibited executing production migrations during this maintenance work.

**How to apply:** Preserve selected DDL, local-only target checks and conditional
predecessor ledger updates. Verify connection policy with offline tests or owned
disposable databases, not by running a repair against production.
