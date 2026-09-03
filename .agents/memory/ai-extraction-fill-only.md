---
name: AI extraction is fill-only
description: Global authority rule for when course scraping may use AI extraction.
---

Across every university, AI extraction is a gap-filling fallback after the normal deterministic static-page pass. Never ask AI for a field that already has a usable deterministic value, and never let AI replace that value.

**Why:** The user requires AI to be used only when needed. Model output can misclassify otherwise authoritative values, such as a published full-course fee period.

**How to apply:** Filter both AI prompts and accepted response keys to canonical fields that remain empty. Preserve the existing conditional rendered-browser fallback because forcing browser rendering for every gap would add substantial latency and provider load.