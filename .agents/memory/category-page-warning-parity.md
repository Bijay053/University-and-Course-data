---
name: Category-page warning parity
description: Safety rules for the live warning that claims discovery retained category pages instead of course pages.
---

The category-page warning must honor the same per-university degree-qualifier override used by staging. A warning tied to a different candidate total than the authoritative runtime job is stale and must not remain visible.

**Why:** CQU intentionally permits subject-like URL slugs because a structured course code identifies detail pages. The warning ignored that override, and the portal retained a 14-link warning while the current job was extracting 182 valid course URLs.

**How to apply:** Keep operator warnings aligned with effective scraper configuration. Reconcile warning counts against persisted runtime totals before displaying categorical claims such as “staged count will be 0.”