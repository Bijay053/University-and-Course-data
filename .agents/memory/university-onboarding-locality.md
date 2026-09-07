---
name: University onboarding locality safety
description: Validation and repair rules for university city metadata discovered during URL onboarding.
---

Navigation labels such as “Maps”, “Directions”, “View map”, “Contact”, and “Find us” are not valid university cities, regardless of which metadata source produced them.

**Why:** Campus-link discovery used anchor text as a fallback city, allowing a “Maps” link to suppress the correct hostname-based city for Macquarie University.

**How to apply:** Normalize and reject generic navigation labels at the shared locality boundary before selecting fallbacks or persisting locations. Treat an existing rejected locality as repairable when verified onboarding yields a valid city, but do not replace a different legitimate city automatically.

Country inference must allow authoritative domain exceptions before generic TLD
rules. Monash uses `monash.edu` but is Australian; its explicitly named overseas
campuses retain their own countries rather than inheriting the institution’s.