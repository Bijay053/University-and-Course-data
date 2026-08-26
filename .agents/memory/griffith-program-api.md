---
name: Griffith program API authority
description: Durable source and fallback rules for Griffith international degree extraction.
---

Griffith degree detail pages are Vue shells. Treat the public v3 program endpoint as the authoritative source for the displayed international title, fee, campus, duration, intakes, IELTS and CRICOS fields.

**Why:** Browser-rendering every shell was slow and costly, while the same structured endpoint used by Griffith's frontend returned the key facts directly and deterministically.

**How to apply:** Derive the program code from the degree URL and fetch the API before generic HTML extraction. If a listed code returns 404, use the same international/2027 Funnelback search result's metadata as a partial fallback rather than dropping the listing; unavailable IELTS/CRICOS must remain blank instead of being guessed.