---
name: Surrey catalogue and English authority
description: Source interpretation for Surrey discovery and English requirements.
---

Treat Surrey's central international English-language page as a conversion
reference, not a university-wide admission requirement. Course pages own the
required IELTS scores; missing course scores must remain unresolved rather than
being filled from a different test's conversion row or invented defaults.

**Why:** The official reference repeats different IELTS offer levels alongside
Cambridge, PTE, and TOEFL equivalents. Flattening it produced IELTS overall 185
and components 162/100/110/59 in the user's worker log.

**How to apply:** Preserve course-owned requirements and keep alternate-test
conversions conditional on an exact matching offer, never a first table match.
Invalidate parsed central caches when changing this interpretation.

Use current undergraduate, taught postgraduate, and research A–Z listings when
checking catalogue coverage. Undergraduate URLs can be unversioned.

**Why:** Requiring a year suffix excluded current undergraduate links. The
configured 400-course floor was not supported by the roughly 249 unique matching
links across the three current lists; degree variants need not have unique URLs.

**How to apply:** Recheck the live lists before changing coverage expectations.
A lower collapse threshold or a bounded discovery result is not proof of a
complete extraction or of eligibility for every discovered course.
