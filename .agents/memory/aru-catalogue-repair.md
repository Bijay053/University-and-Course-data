---
name: ARU catalogue repair authority
description: Why ARU live evidence, sitemap enumeration, and course-owned facts require separate checks.
---

An accepted live-page probe is not proof that the regular scrape can enumerate and stage the catalogue. ARU's public sitemap lists pages under its Azure publishing hostname; map only exact, course-shaped sitemap paths back to the official public host before ordinary safety filters, rather than permitting extraction from the publishing host. Keep the verified public-host recipe above stale generated URL filters.

**Why:** The first production repair correctly recognized three public course pages, then its verification scrape still saw only a few candidates because the sitemap host differed and old generated rules matched the Azure host. The few extracted pages were falsely labelled Online from generic page text while their course-owned tab facts listed physical campuses.

**How to apply:** Check the regular discovery and staging counts after any successful live probe. Validate the final merged recipe against stored overrides, not just clean YAML. Locate ARU's current-course Location in the selected course-options panel under the H1-owned container, not an assumed adjacent H1 sibling or page-wide text; preserve the global online-only rejection for genuinely online offerings.

Forced browser extraction can overwrite a stronger, static course-owned delivery decision even when its rendered-page result came from the bare word “online.” Retain the underlying extractor method when wrapping browser evidence; an official physical campus fact outranks a later generic keyword, but not an explicit course-owned Online label.

**Why:** A production review-only sample found course-header campuses, yet rejected most pages as online-only. The old field trace printed a legacy location alias rather than the actual course-location field, which initially disguised the overwrite. Replaying a saved production page through the full browser path reproduced the wrong Online value; disabling browser rescue did not.

**How to apply:** Trace the full static→rendered merge and both evidence methods before changing a staging guard. Check the canonical course-location field rather than a display alias, and test the rendered path as well as isolated HTML extractors.

The official-catalogue repair fallback is separate from normal discovery: a sitemap may work in normal scraping but fail in repair if repair uses generic course-path heuristics instead of the configured sitemap canonicalization. Keep the publishing host as metadata only, map exact allowed locations to the public host, and retain independent bounded verification rather than claiming a catalogue pass from a small sample.

**Why:** ARU’s normal discovery found hundreds of public URLs, while the fallback’s generic course-path matcher excluded ARU’s study-path course URLs.

**How to apply:** When comparing repair to a fresh scrape, check both discovery implementations and their source-host/URL filtering, not just their shared university recipe.

Bounded repair samples must be chosen after filtering the complete configured sitemap, not from its first N raw locations. Category/navigation URLs often appear first; a valid official source can otherwise produce a failed repair probe before any real course is checked.

**Why:** The initial live fallback had many public-host candidates but sampled only ARU subject-area and guidance pages. Those pages cannot establish course-owned evidence, even though degree detail pages were present later in the same sitemap.

**How to apply:** Keep candidate output bounded but distribute the evidence sample among likely degree details, and separately verify that the sample pages really classify as courses. A healthy sample still does not certify the whole catalogue.