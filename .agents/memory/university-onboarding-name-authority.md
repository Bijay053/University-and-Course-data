---
name: University onboarding name authority
description: Rules for validating and repairing institution names inferred during add-by-URL onboarding.
---

Treat every metadata source consistently: Open Graph site names, application
names, HTML titles, and AI identity results can all contain homepage marketing
copy rather than an official institution name.

**Why:** Separator-only cleanup fixed titles such as “Home | Western Sydney
University” but still accepted a single segment such as “Study at James Cook
University in Queensland.” A later official-name candidate was also blocked
merely because the bad existing phrase already contained “University.”

**How to apply:** Run every metadata source through the same sanitizer. Strip
generic wrappers, prefer authoritative domain mappings when available, and
allow a clean official candidate to replace a longer phrase that contains it as
a complete name. Keep exact regression cases for both separator-based and
single-segment marketing titles.