---
name: Portsmouth course-owned delivery and fee evidence
description: How to avoid Portsmouth navigation, language-policy copy, and UK-only fees contaminating course extraction.
---

Portsmouth's exact-course structured instances are the authority for delivery and study load; its course-owned international tuition row is the authority for international fees. A UK-only fee panel is negative evidence, not a fallback price. An explicitly international low price for a specialist part-time course can be genuine.

**Why:** Page-wide text includes “not taught by Distance Learning” in English-language policy and online-course navigation, which caused on-campus awards to be rejected as online-only. Adjacent UK and international fee rows led to domestic prices being selected, while the unusually low specialist-course price was actually printed for both audiences. Some current pages use plain UK/EU/international tuition lines rather than the accordion template; on such pages generic regex can select the UK row, and previously approved EU prices can be carried into new staged rows even after extraction authority checks.

**How to apply:** Match the structured course URL to the current course before reading its instances. Preserve global online-only and part-time-only eligibility rules; fix their input evidence instead of weakening them. Inspect both plain fee sections and inherited values when auditing final staged fees; a clean extraction-time guard or zero critical quality findings does not prove the staged price belongs to international students. Review low-fee warnings against the owned international row and study load before treating them as extraction errors.