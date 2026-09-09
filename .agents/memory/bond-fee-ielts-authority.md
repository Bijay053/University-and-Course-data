---
name: Bond fee and IELTS authority
description: Safe authority and fallback rules for Bond program fees and English requirements.
---

Bond’s details API program ID is a valid fallback for a missing HTML program code, but an explicit HTML code always wins. Fee precedence is explicit annual amount, then per-semester amount times Bond’s three semesters, then total preserved as `Full Course`. A successful `fees: []` response is an authoritative omission: clear downstream fee guesses and do not inherit an older approved fee.

**Why:** Bond’s live pages vary in identifier markup, and its fee API has three legitimate shapes plus explicit empty results. Treating totals as annual or empty results as permission to guess produced misleading review data.

**How to apply:** Keep amount, term, year, currency, and source evidence atomic. Select 2026 when present, otherwise the newest numeric year. Never let static, AI, defaults, or approved-row preservation refill an authoritative empty response.

Bond’s current IELTS source is program-keyed and uses HTML rowspans. Course-specific entry-requirements pages remain authoritative; central values may fill only missing slots for an exact named program/group, parsed from that row’s own requirement cell.

**Why:** Flattening the central page can borrow a neighboring program’s score or turn one exception into a university-wide default.

**How to apply:** Resolve table rowspans, require exact program/group identity, parse the numeric requirement from the matched row, and let any course-page evidence override central-origin values.