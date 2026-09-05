---
name: Autonomous config repair rollback
description: Safety boundary for agents that validate and persist scraper configuration changes.
---

Autonomous scraper repairs must validate against immutable evidence without mutating staged or production rows. Persist only after field-specific improvement and preservation checks pass.

**Why:** A compare-and-swap apply is not enough. If later processing fails, an unconditional compensating restore can erase a newer operator edit made after the repair was saved.

**How to apply:** Fence the initial write against the exact pre-validation config and fence rollback against the exact config written by that repair. If either comparison fails, preserve the newer config and report the conflict instead of overwriting it.