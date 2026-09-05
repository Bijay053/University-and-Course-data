---
name: CQU sitemap-only discovery
description: Why CQU discovery must bypass its rendered course listing and use the explicit sitemap.
---

CQU discovery must use the explicit course sitemap with a zero-page BFS budget. Keep these verified paths locked against generated and operator overrides.

**Why:** The rendered `/courses` page exposes only two broad `/study` links. Generic BFS misclassifies subject and information pages as course details and consumes the entire discovery deadline before the sitemap supplement can run. The static residential-proxy tier returns the complete sitemap reliably.

**How to apply:** Enter sitemap fallback immediately, filter for two-letter/two-digit higher-education course codes, and do not retain CQU in the global always-supplement host set because that fetches the same sitemap twice.