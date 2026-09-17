---
name: AI extraction is fill-only
description: Global authority rule for when course scraping may use AI extraction.
---

Across every university, AI extraction is a gap-filling fallback after the normal deterministic static-page pass. Never ask AI for a field that already has a usable deterministic value, and never let AI replace that value.

**Why:** The user requires AI to be used only when needed. Model output can misclassify otherwise authoritative values, such as a published full-course fee period.

**How to apply:** Filter both AI prompts and accepted response keys to canonical fields that remain empty. Preserve the existing conditional rendered-browser fallback because forcing browser rendering for every gap would add substantial latency and provider load.

Required-field completeness takes precedence over cost-based coverage. A shared
AI gate may skip enrichment only when fee, English, duration, intake, delivery,
and physical location for non-online courses are all present; taxonomy alone may
still use classification-only mode.

**Why:** A high-value percentage can look complete while omitting a required
publishability field such as campus location, silently disabling both primary
and fallback recovery.

**How to apply:** Use the shared required-field completeness contract before any
coverage heuristic. Preserve explicit per-university AI kill switches and
authoritative online-only early exits as separate higher-level policies.