---
name: Resume count semantics
description: Separate settled URL acknowledgements from staged review records during recovery.
---

A completed URL acknowledgement is not evidence of an imported review row.
Same-child recovered rows are already included in a same-job database count;
only genuine cross-job resume rows may be added to that count.

**Why:** Filter acknowledgements and double-added same-child checkpoints caused
resumed reports to claim multiple imports while preserving only one review row.

**How to apply:** Keep progress offsets separate from imported-row offsets.
When reconciling counters, retain the distinction between same-child recovery
and ordinary cross-job resume. Validate against saved rows after worker
completion and fresh API reload, without modifying preserved review data.