---
name: Global international-audience filters
description: Product policy for domestic-only and online-only course staging.
---

Confirmed domestic-only and online-only courses must be excluded from the
international review queue for every university. Legacy per-university YAML and
database admin settings may contain `enabled: false`, but they cannot opt out
of either filter.

**Why:** The user explicitly chose a fleet-wide policy after a Torrens scrape
admitted courses that should have been excluded through a per-university
override.

**How to apply:** Fix extraction evidence when a course is classified
incorrectly (for example, study-mode signals); never restore a per-university
filter bypass.

UTAS embeds a shared “may not be available” / “see the list of distance courses” advisory on ordinary course pages. Inline links can split the phrase in raw HTML and re-form it only after tag stripping, so cleanup must also run on normalized visible text. Explicit statements inside the International section remain authoritative.

**Why:** Raw-HTML-only cleanup missed the split advisory and falsely rejected mainstream courses, while genuinely unavailable courses still published explicit International-section exclusions.

**How to apply:** Ignore only the bounded shared advisory on UTAS. Preserve explicit course-level “not available to international students” evidence and let the separate all-virtual delivery guard reject genuine online-only courses.