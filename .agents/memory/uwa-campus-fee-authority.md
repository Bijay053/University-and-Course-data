---
name: UWA campus and fee authority
description: Source boundaries for UWA course locations and international annual fees.
---

For UWA, location must come from the current course’s labelled Campus location card. Navigation, related-course content, and general university text are not course-location evidence. An absent campus card remains blank.

**Why:** Page-wide extraction assigned Perth or Sydney/Perth to nearly every result, including courses whose current page did not publish that delivery location.

**How to apply:** Preserve an explicit empty location from the course-owned card path so generic extraction cannot restore a campus guess.

International annual tuition must be resolved through UWA’s official calculator using the exact Course Code from the current course card, the current fee year, and the correct international study-level category.

**Why:** Course pages describe the fee calculation and link the calculator but do not embed the amount, so page-only and AI extraction leave every fee blank.

**How to apply:** Keep amount, AUD currency, Annual period, fee year, exact course code, and calculator evidence atomic. A failed or malformed lookup leaves the fee blank with a review warning; it never permits a guessed amount.