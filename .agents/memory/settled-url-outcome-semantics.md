---
name: Settled URL outcome semantics
description: How worker recovery must interpret completed URL checkpoints that carry explicit error outcomes.
---

A URL in a durable completed checkpoint is settled, not necessarily successful. If its persisted outcome is `error`, worker recovery must keep it unresolved and available for redelivery rather than treating it as prior success or filtering it from the resumed work list.

**Why:** A worker can die after committing an exhausted fetch/extraction error but before final diagnostics. The completed checkpoint survives, and interpreting that list alone makes the failed URL appear resolved or produces a zero-work successful redelivery.

**How to apply:** Whenever recovery reads completed URL acknowledgements, pair them with durable outcome and exclusion provenance. Error outcomes remain unresolved and retryable; do not use the completed list alone for success classification or resume suppression.

Do not durably settle exact submitted report URLs while catalogue filters are temporarily removing them before restoration.

**Why:** A valid Leeds Trinity report target was restored to the extraction work list but its earlier exclusion checkpoint made the resume gate skip it. Recovery finished with one processed URL, zero staged courses, and no extraction error.

**How to apply:** During catalogue filtering, checkpoint against the logically retained submitted targets. After their restoration boundary, use the actual work set so global eligibility exclusions remain durable. Test the checkpoint and resume effects, not just the restoration helper.