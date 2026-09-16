---
name: Duration value-unit atomicity
description: Why deterministic duration values and their units must remain paired through later extraction merges.
---

Treat a deterministic duration value and its normalized unit as one atomic result. If a later merge corrupts only the unit, restore the deterministic unit only when the final numeric duration still matches the original paired value.

**Why:** A week-based duration was converted to months, but unrelated text later replaced only its unit. The year-based sanity check then interpreted the unchanged month count as years and cleared valid data.

**How to apply:** Preserve normalized duration pairs from deterministic extractors through final validation. Never restore an earlier unit after another source changes the numeric duration.