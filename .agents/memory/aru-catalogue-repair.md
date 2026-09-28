---
name: ARU catalogue repair authority
description: Why ARU live evidence, sitemap enumeration, and course-owned facts require separate checks.
---

An accepted live-page probe is not proof that the regular scrape can enumerate and stage the catalogue. ARU's public sitemap lists pages under its Azure publishing hostname; map only exact, course-shaped sitemap paths back to the official public host before ordinary safety filters, rather than permitting extraction from the publishing host. Keep the verified public-host recipe above stale generated URL filters.

**Why:** The first production repair correctly recognized three public course pages, then its verification scrape still saw only a few candidates because the sitemap host differed and old generated rules matched the Azure host. The few extracted pages were falsely labelled Online from generic page text while their course-owned tab facts listed physical campuses.

**How to apply:** Check the regular discovery and staging counts after any successful live probe. Validate the final merged recipe against stored overrides, not just clean YAML. Locate ARU's current-course Location in the selected course-options panel under the H1-owned container, not an assumed adjacent H1 sibling or page-wide text; preserve the global online-only rejection for genuinely online offerings.