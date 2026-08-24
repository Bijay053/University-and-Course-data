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