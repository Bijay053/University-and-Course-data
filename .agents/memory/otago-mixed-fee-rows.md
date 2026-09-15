---
name: Otago mixed fee rows
description: Fee-audience handling for University of Otago qualification pages.
---

Otago qualification pages publish audience-specific values in camelCase meta
fields such as `internationalFeesMin` and `internationalFeesYear`; visible fee
rows may be absent even in rendered HTML. When visible Domestic and
International rows do exist inside one shared “Estimated fees” block, an amount
selected from the explicitly labelled International row must not be rejected
merely because “Domestic” also occurs elsewhere in that block.

**Why:** Label-only extraction left published metadata fees blank, while the
final YAML rejection pass could also match a neighbouring Domestic label and
clear a correctly selected International amount.

**How to apply:** Read the international minimum and year directly from
structured metadata, preserve its explicit international provenance, and keep
the exact International fee label as a positive audience marker. Preserve
missing metadata or “To be confirmed” as blank rather than inventing a value.