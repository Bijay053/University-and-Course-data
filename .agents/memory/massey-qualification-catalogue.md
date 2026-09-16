---
name: Massey qualification catalogue
description: The authoritative discovery entry point and URL boundary for Massey degrees.
---

Use Massey’s static `/study/all-qualifications-and-degrees/` index for qualification discovery. Treat `/study/courses/` as a paper/course-code search, not the degree catalogue, and extract only child detail URLs beneath the qualification index. Route full detail extraction through the static proxy rather than direct origin requests.

**Why:** Generic crawling from `/study/courses/` spent its page budget on navigation and planning content, then found only a small accidental subset of qualifications. The dedicated index exposes the complete catalogue in one static response. Production’s origin IP is blocked after roughly 60–70 direct detail requests even when requests are sequential; lowering concurrency alone does not prevent the partial run.

**How to apply:** Seed the dedicated index, force its child paths as candidates, keep the index listing-only, and apply a final detail-path allow-list. Use static proxy extraction and suppress the unhelpful per-course browser pass. Read the explicitly labelled international tuition row on each qualification page as the fee authority; use the central schedule only to fill missing detail fees. Keep degree-level English defaults.