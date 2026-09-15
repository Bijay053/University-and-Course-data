---
name: Otago mixed fee rows
description: Fee-audience handling for University of Otago qualification pages.
---

Otago qualification pages publish Domestic and International fee rows inside
one shared “Estimated fees” block. An amount selected from the explicitly
labelled International fee row must not be rejected merely because “Domestic”
also occurs elsewhere in that block.

**Why:** The browser correctly extracted published international amounts, but
the final YAML rejection pass matched the neighbouring Domestic label and
cleared them, leaving most staged qualifications with Missing Fee warnings.

**How to apply:** Retain the broad domestic safeguards, but configure the exact
International fee label as the positive audience marker. Preserve “To be
confirmed” as missing rather than inventing a numeric fee.