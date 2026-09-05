---
name: UniSC fee and English authority
description: Durable source-authority rules for UniSC international fees and level-aware English requirements.
---

For UniSC, only an exclusive `international` audience fee panel is authoritative for course-level international tuition. Full-fee-paying, units-of-study, and CSS student-contribution schedules are unit or domestic/CSP prices, not degree-program international tuition. If a course has no exclusive international panel, retain the authoritative “no international fee” signal through AI and fallback stages.

**Why:** Multiple differently named UniSC fee PDFs produced plausible low amounts, and an AI fallback could refill a domestic amount after deterministic extraction had correctly found no international panel.

**How to apply:** Keep the central-page and per-course PDF paths on the same non-tuition policy, and make authoritative no-international evidence block both AI requests and merge-time writes. Parse UniSC’s column-keyed English table by qualification level and version the cached parser output whenever its shape or semantics change.