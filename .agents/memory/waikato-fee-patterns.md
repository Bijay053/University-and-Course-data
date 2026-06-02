---
name: Waikato fee table patterns
description: central_pages.py fixes for Waikato's 8-table fee schedule with degree-type headers and verbose major lists
---

## Pattern A — degree-type header rows
Some fee tables use a row where the first cell is a degree type (e.g. "Graduate Diploma") followed by subject-only rows ("Accounting"). `_DEGREE_HDR_MAP` maps these header strings to canonical degree-name prefixes; the parser prepends the prefix to bare subject rows.

## Pattern B — multi-column degree-type tables
Tables with two fee columns, each headed by a degree type (e.g. "PGDip | PGCert"), emit one record per degree-type column per subject row.

## Major-list annotation stripper (strip regex)
Waikato stores rows like:
- `"Bachelor of Business (BBus) All major subjects: Accounting, …"` — colon directly after keyword
- `"Bachelor of Engineering with Honours (BE(Hons)) All major subjects, years 1-3: Chemical …"` — extra qualifier before colon

Strip regex: `r"\s+All\s+(?:major\s+)?(?:subjects?|streams?|majors?|options?)[^:]*:.*$"` with `re.IGNORECASE | re.DOTALL`.

The key change vs the original: `\s*:` → `[^:]*:` so optional qualifiers like ", years 1-3" before the colon are tolerated. Without this, "Bachelor of Engineering with Honours" couldn't match its fee row (score 16.9 → miss at threshold 80).

**Why:** token_sort_ratio sorts both strings' tokens alphabetically and compares. Long subject lists add dozens of tokens that swamp the short course name tokens.

## Abbreviation stripping in match_central_fee
Waikato fee table row names include trailing abbreviations like "(BHealth)", "(BBus)", "(BE(Hons))". When the scraped course name omits the abbreviation, token_sort_ratio penalises the extra token:
- `"Bachelor of Health (BHealth)"` vs `"Bachelor of Health"` → score 78.3 (miss at threshold 80)

Fix: in `_score()`, also try with trailing abbreviation stripped from the pattern (using `r"\s*\([a-zA-Z][a-zA-Z()\s]{0,25}\)\s*$"` with `re.IGNORECASE`). Take `max(s1, s2)`. This gives score 100 (exact) for "Bachelor of Health".

**Why:** Abbreviations are never in the scraped course name. They should not count against the match score.

## IELTS same for all courses — degree_level_defaults + pg_skip interaction
Waikato's central English page is UG-only (`/study/apply/undergraduate-international/...`). It returns flat UG values: IELTS 6.0 / PTE 50 / TOEFL 80 / Duolingo 105. These were applied to ALL courses including PG ones (which need 6.5/58/90).

The YAML had `degree_level_defaults: postgraduate: ielts: 6.5` but those defaults only fill empty/unproven slots. The central page fills them first (with "proven" evidence = source_url + snippet), blocking the defaults.

Fix in `single_course.py` Path 2: if `degree_level_defaults` has a `postgraduate` entry AND the course is PG tier, auto-enable `pg_skip`. PG courses skip flat central English; the `degree_level_defaults` block then applies correct PG values.

**How to apply:** Any university whose central English page is UG-specific but YAML has `degree_level_defaults.postgraduate` configured will automatically benefit from this fix.
