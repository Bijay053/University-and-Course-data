---
name: Otago mixed fee rows
description: Fee-audience handling for University of Otago qualification pages.
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