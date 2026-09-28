---
name: BCU production recipe identity
description: Why Birmingham City University scraper changes must cover two recipe identities.
---

BCU's production university record has a different ID from the development ID-specific recipe. Production therefore loads the slug-level recipe; changing only the ID-specific recipe does not fix live scrapes.

**Why:** A location fix initially passed against the development recipe but failed for the production recipe. The live course-facts panel can also name multiple sites separated by slashes, so normalizing to the first familiar campus silently loses correct locations.

**How to apply:** When changing BCU extraction, test both recipe identities and compare staged location values against current course-owned sources. Preserve verified multi-site values as a whole rather than deriving a campus from a substring. If the key-facts panel has no Location row, leave the field blank unless an explicitly labelled course-owned source verifies the location (for example, a course's Schedule section or its linked programme specification). Never infer from general campus facilities or neighbouring courses.