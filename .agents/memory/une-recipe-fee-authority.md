---
name: UNE recipe and fee authority
description: Durable safeguards for UNE config selection and international tuition evidence.
---

Keep UNE's shared-slug and ID-suffixed scraper recipes semantically aligned. Treat only fee evidence that explicitly identifies international tuition as authoritative; CSP, student-contribution, amenities, scholarship, award, and grant amounts must never populate international tuition. When no trustworthy amount is present, keep the course eligible for human fee review with a blank fee.

**Why:** A production database ID selected an old shared-slug stub instead of the hardened ID-suffixed recipe, restoring obsolete year-based discovery and a three-second deadline. After current discovery was restored, generic fee extraction exposed domestic CSP and scholarship amounts on the same official course pages and could mislabel them as international tuition.

**How to apply:** When changing UNE discovery, transport, deadlines, or fee policy, verify both recipe selectors with the production-style loader paths. Audit selected fee evidence, not just the numeric output. Require explicit international-tuition wording and fail closed to the central-fee review path for ambiguous values.