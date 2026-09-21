---
name: AI repair live-evidence seeds
description: How bounded autonomous repair obtains valid course-page evidence for current and historical scrape jobs.
---

One-click repair must include known staged course URLs in its bounded live-evidence sample even when the parent job discovered many URLs. If the parent job's review rows were replaced by a newer scrape, the latest staged review set for the same university may supply live-evidence seeds only.

**Why:** A rejection storm can discover many navigation pages while staging a small valid subset. Historical job rows can then disappear when a newer scrape replaces the review set. Sampling only the homepage and pipeline pass/drop lists can produce zero course evidence and block every safe repair attempt.

**How to apply:** Keep config mutation fenced to the requested repair session, but seed its read-only live probe from exact-job staged URLs first and the newest same-university review set when those rows no longer exist. Acceptance still requires fresh course classification and normal validation.