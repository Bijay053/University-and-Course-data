---
name: Prompt assistant scope and grounding
description: User-selected scope and why researched citations use immutable source excerpts.
---

## Scope
The user chose “Scraping and course reviews” for the prompt assistant and said the portal already has the OpenAI API.

**Why:** They want it to find correct information, fix wrong fees/IELTS and missing courses, and avoid unexplained waiting—not a separate general administration chatbot.

**How to apply:** Reuse the existing OpenAI connection and guarded scraper/review operations. Keep research read-only until an explicit corrective-action confirmation; never let generated numbers bypass deterministic source authority or approval.

## Grounding
Ask the model to select immutable excerpt IDs, not to reproduce quotations.

**Why:** In live tests, even an explicit verbatim-quotation instruction produced apparently plausible quotations stitched from nonadjacent table rows and course facts. Strict text validation correctly rejected them but made otherwise useful research fail.

**How to apply:** Generate actual source excerpts server-side, let the model select their IDs, then retrieve and validate the original excerpt. Do not relax validation to accept synthesized extractor summaries as quotations.
