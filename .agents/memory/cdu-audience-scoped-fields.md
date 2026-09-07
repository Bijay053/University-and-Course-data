---
name: CDU audience-scoped course fields
description: Authority rules for CDU international fees, locations, and study mode.
---

CDU course pages publish domestic and international values in the same static HTML and append many related-course cards. Scope fees and locations to the current course’s explicit `data-student-type="international"` blocks.

**Why:** Whole-page extraction selected domestic per-unit/CSP amounts and locations belonging to related cards. The official current-course international fee and campus values were present but ignored.

**How to apply:** Read annual tuition from the international child of the current course’s Fees accordion. Read location from the current key-fact location block’s international child, remove Online from physical location, and derive mode from the remaining physical/online combination.

CDU's yearless `/study/course/...` route can omit the international fee block. Normalize course URLs to the active calendar-year query before extraction.

**Why:** The same course returned no fee without a query but included its official annual international tuition at `?year=<current year>`.

**How to apply:** Add the current catalogue year at the shared extraction entry point when a CDU course URL has no existing `year` query; preserve an explicit year unchanged.