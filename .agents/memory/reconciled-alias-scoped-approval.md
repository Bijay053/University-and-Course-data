---
name: Reconciled aliases and scoped approval
description: How preserved old course IDs interact with future campus-scoped publications
---

An old course retained as a reviewed alias must not count as an unresolved legacy collision during a later scoped approval of the canonical course. A same-source course with no verified alias remains a collision and must still block publication.

**Why:** Reconciliation preserves old course IDs rather than deleting their rows. A guard based only on the old rows' empty offering identity would reject every future scrape of the reconciled award.

**How to apply:** Compare collision candidates against persisted alias mappings, not URL/name alone. Test both the unmapped rejection and a subsequent canonical approval after mapping.