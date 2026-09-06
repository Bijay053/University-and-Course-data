---
name: UniSC fee and English authority
description: Durable source-authority rules for UniSC international fees and level-aware English requirements.
---

For UniSC, only an exclusive `international` audience fee panel is authoritative for course-level international tuition. Full-fee-paying, units-of-study, and CSS student-contribution schedules are unit or domestic/CSP prices, not degree-program international tuition. If a course has no exclusive international panel, retain the authoritative “no international fee” signal through AI and fallback stages.

UniSC English Table 1 is the standard by study level. Plain Diploma, Advanced Diploma, and Associate Diploma use Undergraduate only when no explicit diploma profile exists; Graduate Diploma remains Postgraduate. Table 2 profiles apply only to exact normalized program-title matches and outrank image OCR. Never flatten Table 2 into a university-wide default.

**Why:** Multiple differently named UniSC fee PDFs produced plausible low amounts, and an AI fallback could refill a domestic amount after deterministic extraction had correctly found no international panel. Unrelated promotional imagery exposed a 7.0 IELTS value that vision OCR assigned to a diploma, while a missing diploma bucket fell through to the flat postgraduate profile.

**How to apply:** Keep the central-page and per-course PDF paths on the same non-tuition policy, and make authoritative no-international evidence block both AI requests and merge-time writes. Parse UniSC’s standard and named-program English tables separately, use exact title matching for exceptions, and version cached parser output whenever its shape or semantics change.