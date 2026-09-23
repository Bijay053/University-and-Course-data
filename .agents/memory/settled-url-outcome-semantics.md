---
name: Settled URL outcome semantics
description: How worker recovery must interpret completed URL checkpoints that carry explicit error outcomes.
---

A URL in a durable completed checkpoint is settled, not necessarily successful. If its persisted outcome is `error`, worker recovery must keep it unresolved and available for redelivery rather than treating it as prior success or filtering it from the resumed work list.

**Why:** A worker can die after committing an exhausted fetch/extraction error but before final diagnostics. The completed checkpoint survives, and interpreting that list alone makes the failed URL appear resolved or produces a zero-work successful redelivery.

**How to apply:** Whenever recovery reads completed URL acknowledgements, pair them with durable outcome and exclusion provenance. Error outcomes remain unresolved and retryable; do not use the completed list alone for success classification or resume suppression.