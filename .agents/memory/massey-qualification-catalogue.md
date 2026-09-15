---
name: Massey qualification catalogue
description: The authoritative discovery entry point and URL boundary for Massey degrees.
---

Use Massey’s static `/study/all-qualifications-and-degrees/` index for qualification discovery. Treat `/study/courses/` as a paper/course-code search, not the degree catalogue, and extract only child detail URLs beneath the qualification index.

**Why:** Generic crawling from `/study/courses/` spent its page budget on navigation and planning content, then found only a small accidental subset of qualifications. The dedicated index exposes the complete catalogue in one static response.

**How to apply:** Seed the dedicated index, force its child paths as candidates, keep the index listing-only, and apply a final detail-path allow-list so unrelated `/study/` pages never reach extraction.