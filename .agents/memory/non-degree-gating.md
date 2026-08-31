---
name: Fleet-wide non-degree gating
description: Safety and accounting rules for excluding obvious non-degree candidates before expensive scrape work.
---

Use conservative three-way classification: clear degree evidence preserves a
candidate even under a CPD-like URL, high-confidence non-degree evidence may
reject it, and everything else fails open. Never treat absence of a degree
prefix as rejection evidence.

**Why:** Real award-bearing MSc and doctorate routes can live inside CPD
catalogues. Raw HTML also contains hidden modals, inert component templates,
navigation, footers, and related-course cards that can falsely look like
course-owned “short course” evidence.

**How to apply:** Filter clear URL/title cases after discovery has fully merged
its providers but before final extractable counts. For an ambiguous fetched
page, accept non-degree page evidence only from visible structured label/value
elements inside the main/article course-detail region; strip hidden and inert
nodes first. Preserve discovery-title allow/degree decisions into extraction.
Keep prefetch-filter counts in discovery/performance diagnostics because
post-filter candidates define total_found; reserve summary.skipped and staging
skip reasons for candidates that actually entered extraction.