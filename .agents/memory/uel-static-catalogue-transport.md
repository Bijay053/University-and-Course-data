---
name: UEL catalogue and award-route authority
description: UEL transport and ownership constraints for multi-award course pages.
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

Treat UEL award variants as independent courses, but never treat intake tabs,
applicant types or attendance rows as independent awards. Requirements belong
to the labelled award, not the shared page title. Undergraduate pages may have
a second labelled chooser inside one entry-requirements dialog.

**Why:** UEL repeats the base MSc/MA title inside placement-year/MFA options and
can publish different English requirements for routes sharing a page. Generic
enrichment of a sparse route would reintroduce sibling facts. The deliberate
tradeoff is to leave unproven route facts missing rather than fill them from
the parent page, institutional defaults or a sibling course.

**How to apply:** Keep route identity stable through approval and re-extraction;
retain route-owned omissions rather than restoring historical shared-page
values. Require explicit Home-only evidence before excluding a route as
domestic: an unfamiliar option layout is an extraction error, not eligibility
evidence. Check mixed IELTS skill floors against the actual labelled panel;
“6 in writing/speaking; 5.5 in listening/reading” is not a 6 all-band floor.