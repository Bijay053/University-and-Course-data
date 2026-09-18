---
name: UEL static catalogue transport
description: Reliable discovery transport for the University of East London catalogue.
---

Use the two UEL undergraduate and postgraduate catalogue pages through
Scrape.do with rendering disabled. Treat their exact undergraduate and
postgraduate course-detail URL shapes as the discovery authority.

**Why:** In September 2026, direct requests returned Cloudflare 403 responses,
Playwright received pages with navigation but zero course anchors, and Wayback
returned no course-like URLs from 10,000 records. Static Scrape.do returned the
complete server-rendered catalogue; JavaScript rendering added no links.

**How to apply:** Keep generic browser discovery and Wayback disabled for UEL.
When its catalogue count changes, verify both static listing pages before
changing extraction logic or re-enabling expensive discovery tiers.

UEL course-option rows are audience- and attendance-scoped. For duration, use
the row that explicitly pairs International Applicant with Full time, and read
its adjacent duration value. Normalize “Prof Doc” course-title suffixes as
Doctorate.

**Why:** Flattened UEL pages can expose unrelated one-year values before the
international full-time option, while professional doctorate titles often omit
the full word “Doctorate.” This produced a one-year duration and blank degree
level for valid three-year doctoral courses.

**How to apply:** Keep this rule exact-host scoped and prefer the structured
Course options row over generic page-wide duration matching. Verify both the
MPhil/PhD route and Prof Doc title forms when changing UEL extraction.