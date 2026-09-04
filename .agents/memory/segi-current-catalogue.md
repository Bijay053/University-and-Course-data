---
name: SEGi current catalogue transport
description: Authoritative source, transport boundary, and data limitations for SEGi University scraping.
---

Use `university.segi.edu.my` as SEGi University’s authoritative current catalogue. The legacy `www.segi.edu.my/course/...` archive is not an acceptable extraction fallback because its pages are obsolete and commonly omit required fields.

**Why:** The current host presents an incomplete TLS certificate chain. Rendered proxy calls are slow and intermittently fail with proxy rotation errors, while direct requests with certificate verification disabled are fast and complete. The exception must remain exact-host and HTTPS-only, and redirects must be revalidated before following them.

**How to apply:** Discover from the current site map, allow only exact current course URLs, and never fall through to Wayback or proxy transports for this host. Treat explicit “Online Mode” titles as authoritative Online delivery. Current official pages do not publish programme tuition, so leave fees blank for review rather than inferring them from visa, application, or ancillary charges.