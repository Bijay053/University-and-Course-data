---
name: LSBU render and delivery evidence
description: Why LSBU extraction needs a constrained browser fallback and course-owned online labels.
---

LSBU course pages are accessible through a rendered proxy, while direct requests hit bot protection. On these large rendered pages, the separate per-course browser pass added no missing fields in the observed scrape and consumed the per-course deadline. Keep that pass disabled unless a fresh source comparison proves it supplies an essential field.

**Why:** A large LSBU scrape ended with many per-course timeouts, and the browser passes visible in its log filled nothing. Page-wide study-mode rules also classified campus courses as Online, causing erroneous online-only skips. Broadly disabling Online detection would create the opposite error.

**How to apply:** Limit render concurrency, avoid redundant browser work, and preserve explicit course-owned delivery labels while ignoring utility/navigation text. Treat archived URL discovery counts as candidates, not proof of a current catalogue. Verify recovery with a bounded fresh run before claiming the timeout problem is solved.