---
name: Re-extraction evidence promotion
description: When equal-valued fields must still replace old evidence during in-place re-extraction.
---

Treat an equal-valued field gaining selected source evidence as a provenance change, even when the old evidence rows were all unselected.

**Why:** A correct re-extraction can confirm a value that was previously inherited or marked for review. If refresh requires an existing selected row, the confirmed value stays attached to stale unselected evidence and continues to look unresolved.

**How to apply:** For unchanged payload values, compare the fresh selected evidence with the old selected evidence. Refresh when provenance differs or when no old selected evidence exists; preserve reviewer validation only when the normalized value is unchanged.