---
name: Otago structured qualification metadata
description: Authority rules for University of Otago qualification-page metadata.
---

Otago qualification pages can publish audience-specific values in structured
metadata even when visible fee rows are absent in rendered HTML. When visible
Domestic and International rows do exist inside one shared “Estimated fees”
block, an amount selected from the explicitly labelled International row must
not be rejected merely because “Domestic” also occurs elsewhere in that block.

**Why:** Label-only extraction left published metadata fees blank, while the
final YAML rejection pass could also match a neighbouring Domestic label and
clear a correctly selected International amount.

**How to apply:** Treat explicit international metadata as authoritative,
preserve its audience provenance, and let it outrank neighbouring Domestic
content. Preserve missing metadata or “To be confirmed” as blank rather than
inventing a value.

Otago also publishes course-owned start dates and locations in named metadata
that may not survive visible-text compaction. These fields outrank page-wide
campus navigation and AI guesses; an explicitly empty metadata field remains
blank, while an absent metadata field may use normal fallbacks.

**Why:** Broad campus text caused nearly every qualification to receive the
same four-city location, while compacted text omitted valid start dates.

**How to apply:** Read qualification metadata before generic extraction, keep
empty distinct from absent, and lock authoritative empty fields against all AI,
browser, and default aliases.