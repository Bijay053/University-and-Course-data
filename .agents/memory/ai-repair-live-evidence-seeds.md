---
name: AI repair live-evidence seeds
description: How bounded autonomous repair obtains valid course-page evidence for current and historical scrape jobs.
---

One-click repair must include known staged course URLs in its bounded live-evidence sample even when the parent job discovered many URLs. If the parent job's review rows were replaced by a newer scrape, the latest staged review set for the same university may supply live-evidence seeds only.

**Why:** A rejection storm can discover many navigation pages while staging a small valid subset. Historical job rows can then disappear when a newer scrape replaces the review set. Sampling only the homepage and pipeline pass/drop lists can produce zero course evidence and block every safe repair attempt.

**How to apply:** Keep config mutation fenced to the requested repair session, but seed its read-only live probe from exact-job staged URLs first and the newest same-university review set when those rows no longer exist. Acceptance still requires fresh course classification and normal validation.

Repair diagnosis must distinguish URL-filter loss from later staging rejection, and completeness from validity.

**Why:** A high field fill rate concealed critically invalid tuition values, while a low staged count was incorrectly blamed on URL filtering even though nearly all URLs passed that filter.

**How to apply:** Never substitute imported/staged counts for post-filter counts in legacy evidence. Include critical-quality issues and affected URLs in repair assessment; populated invalid values cannot justify a healthy no-change result. Keep safe filter-tightening proposals subject to validation rather than suppressing all URL changes.

Bounded live probes must preserve staged seed priority across every fetched page, not just the homepage, and must not demand rescue of intentionally excluded variants.

**Why:** ULaw related links displaced remaining staged seeds with online variants. Recognizing those pages as courses then incorrectly demanded discovery changes to admit them; a filtered funding page also kept repair unaccepted.

**How to apply:** Reuse proven intentional exclusions for probe targets and positive evidence. Unknown admitted pages still need review; unknown pages already rejected by effective filters do not independently justify relaxing discovery. Course recognition is not eligibility approval.

When no staged or previously passing course URLs exist, a configured official course sitemap can supply bounded live-evidence candidates even if the failed job discovered many URLs. Do not treat homepage navigation or asset links as substitutes for fresh course-owned evidence.

**Why:** A Portsmouth repair used its entire six-page probe on the homepage, dead media URLs, and broad study pages while the configured sitemap still contained hundreds of current course pages. Discovery count alone prevented the older sitemap seed path.

**How to apply:** Spend a slot within the existing page budget on the configured official sitemap, filter its locations through the effective course URL gates, and independently fetch and classify a small spread of course details. A course-picker option whose name contains “International” is not an international-audience selector.

Do not let missing-field rows consume every live-probe slot. Reserve some slots
for better-populated staged candidates, while still classifying them afresh.

**Why:** Contaminated historical reviews can contain prospectus and outreach
pages with every field missing. Prioritizing gaps alone repeatedly probes those
pages and produces zero positive course evidence despite valid courses nearby.

**How to apply:** Mix gap-focused candidates with a small spread of populated
records within the same page budget. Stored completeness is only a sampling
hint, never proof of course identity or international eligibility.

Prefer recent same-university review URLs for probe anchors even when the
original failed job still has rows; keep the original job as the comparison
baseline.

**Why:** Older Hope rows retained retired course URLs after a fresh verification
had already stored usable replacements. Checking only whether old rows existed
made retries repeatedly fetch the retired URLs.

**How to apply:** Use recency only to choose bounded probe candidates. Re-fetch
and classify every candidate; never promote stored rows into live authority.

When a historical job no longer owns review rows, use one current review
cohort consistently for field diagnosis, baseline presence, and snapshot
replay—not just for live URL sampling. Preserve the original repair's ownership.

**Why:** Production retries passed the URL checks and exited before field
repair because the original job had no rows, even though the current review
contained widespread missing fees.

**How to apply:** Passing URL filters is not a field-quality verdict. Use actual
field-presence metrics rather than non-null SQL counts (empty arrays are not
populated intakes), and replay rules against the diagnosed cohort's snapshots.