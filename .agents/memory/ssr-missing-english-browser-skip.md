---
name: SSR courses with missing English
description: Why fully server-rendered course pages can still waste time in the generic per-course browser fallback.
---

On a fully server-rendered university, merely leaving all course-specific English-test slots empty can still enter the generic per-course browser path even when browser rendering is not explicitly forced. For a proven SSR host, use the per-university browser skip and rely on its central English requirements source instead.

**Why:** A live Murdoch course returned the same fee, duration, intake, location, and mode with and without Playwright, but the generic missing-English path added a 30-second `networkidle` timeout. Explicitly skipping the per-course browser reduced the same live extraction from 40.72s to 6.42s without changing those fields.

**How to apply:** When a host is documented and verified as fully server-rendered, browser output adds no fields, and central English requirements are configured, set `skip_per_course_browser: true`. Do not apply this to JS-hydrated international panels such as UOW when the international fee is missing.