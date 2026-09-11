---
name: Bond fee and IELTS authority
description: Safe authority and fallback rules for Bond program fees and English requirements.
---

Bond identifiers must be paired from the current course’s main program element. The details API code is a valid fallback when HTML omits it; if both exist they must agree before querying fees. Fee precedence is explicit annual amount, then per-semester amount times Bond’s three semesters, then total preserved as `Full Course`. A successful `fees: []` response blocks generic/AI guesses and older approved fees, but Bond’s course-owned `/fees` tab may override it when the international audience block publishes an exact total program fee.

**Why:** Bond’s live pages vary in identifier markup, and its legacy fee API may return `fees: []` for a current program whose official Fees tab publishes the current international total. Treating totals as annual, borrowing the domestic audience block, or letting generic guesses fill an empty result produced misleading review data.

**How to apply:** Keep amount, term, year, currency, and source evidence atomic. Select 2026 when present, otherwise the newest numeric year. Scope Fees-tab parsing to the current course’s `Program fees` section and its international audience block; preserve “total program fee” as `Full Course`. Never let generic static parsing, AI, defaults, or approved-row preservation refill an authoritative empty response. Bond’s legitimate totals can exceed generic warning ceilings, so allow them only under this exact audience-scoped wording.

Bond location and study mode must come from course-owned structured page evidence, not university-wide campus defaults or page-wide keyword scans.

**Why:** Bond pages repeat general online and Gold Coast marketing copy. Treating that chrome as course evidence made unrelated programs all appear “Blended” at one fabricated location and created conflicts with their structured delivery facts.

**How to apply:** Use current course-keyed API offerings for location and mode. A successful empty offerings response must clear generic defaults; transport failure is not an authoritative empty response. Preserve genuine source conflicts for review.

Bond’s current IELTS source is program-keyed and uses HTML rowspans. Course-specific entry-requirements pages remain authoritative; central values may fill only missing slots for an exact named program/group, parsed from that row’s own requirement cell.

**Why:** Flattening the central page can borrow a neighboring program’s score or turn one exception into a university-wide default.

**How to apply:** Resolve table rowspans, require exact program/group identity, parse the numeric requirement from the matched row, and let any course-page evidence override central-origin values.

Packaged Bond pages contain one main `program-detail` element per component,
all sharing the same details API id but carrying different program codes. Query
every paired code: sum the component durations for the standard package
duration and, only when every component publishes a total, sum those totals
and preserve the result as `Full Course`. Do not combine differing semester
fees into a synthetic annual amount. Offerings are the course-owned authority
for current campus and delivery mode; a successful details response with no
offerings must clear page-wide location/mode guesses rather than inheriting
Bond footer or marketing copy.