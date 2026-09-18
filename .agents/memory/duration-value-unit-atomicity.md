---
name: Duration value-unit atomicity
description: Why deterministic duration values and their units must remain paired through later extraction merges.
---

Treat a deterministic duration value and its normalized unit as one atomic result. If a later merge corrupts only the unit, restore the deterministic unit only when the final numeric duration still matches the original paired value.

**Why:** A week-based duration was converted to months, but unrelated text later replaced only its unit. The year-based sanity check then interpreted the unchanged month count as years and cleared valid data.

**How to apply:** Preserve normalized duration pairs from deterministic extractors through final validation. Never restore an earlier unit after another source changes the numeric duration.

Exercise the complete AI-to-persistence boundary with realistic fallback response
shapes, not only static payload-key scans or isolated normalization helpers.

**Why:** Static contract tests passed while a full live catalogue lost otherwise
valid courses to intermediate AI duration aliases leaking into final validation.
Removing those aliases must not weaken the shared persisted-field contract or
change the authority of an already valid duration pair.

**How to apply:** For changes to AI field translation, include end-to-end
extraction tests that stub the model response, retain final payload validation,
and check both missing-field fills and conflicting deterministic values.