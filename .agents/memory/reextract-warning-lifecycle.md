---
name: Re-extraction warning lifecycle
description: How successful re-extraction should remove resolved warning metadata without hiding unrelated review concerns.
---

Re-extraction must clear an old warning only when the new payload contains the field or condition proving that specific warning is resolved; it must preserve unrelated warnings.

**Why:** Treating an omitted warning list as “clear everything” removed legitimate independent review warnings, while preserving the whole old list left false `suspicious_duration` alerts after duration was corrected.

**How to apply:** Define field-specific resolution rules when adding warning cleanup. A fresh valid duration clears `suspicious_duration`; a fresh fee clears `fee_section_detected_fee_blank`; clear `confidence_low` variants only when the recomputed course score passes its threshold. Preserve every unrelated warning.