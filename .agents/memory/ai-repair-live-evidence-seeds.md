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