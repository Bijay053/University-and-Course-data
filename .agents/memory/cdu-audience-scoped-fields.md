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

CDU duration is authoritative only from the current course's international `block-course-key-fact-duration` child.

**Why:** The generic parser selected “0.5 year of prior study” from admission requirements and then correctly nullified it against the bachelor floor, leaving valid durations blank.

**How to apply:** Parse the international full-time year value from the key-details duration block, ignore domestic/related-course values, and exempt this direct source from the generic bachelor minimum-duration floor.

CDU VET pages use a different duration contract: the headline year value is in the current course's `block-course-key-fact-duration-vet`, while its international child confirms student-visa holders must study internally full time.

**Why:** The international child contains no number, so requiring the higher-education phrase left valid VET durations blank; whole-page fallback can select related-course durations.

**How to apply:** Anchor to the current `#key-details` VET block, pair its headline years with the international full-time statement, and reject related-course candidates. VET student-visa fees use “commencing student visa holders” wording inside the international Fees child.