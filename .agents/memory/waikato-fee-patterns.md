---
name: Waikato fee table patterns
description: central_pages.py fixes for Waikato's 8-table fee schedule with degree-type headers and verbose major lists
---

## Pattern A — degree-type header rows
Some fee tables use a row where the first cell is a degree type (e.g. "Graduate Diploma") followed by subject-only rows ("Accounting"). `_DEGREE_HDR_MAP` maps these header strings to canonical degree-name prefixes; the parser prepends the prefix to bare subject rows.

## Pattern B — multi-column degree-type tables
Tables with two fee columns, each headed by a degree type (e.g. "PGDip | PGCert"), emit one record per degree-type column per subject row.

## Major-list annotation stripper
Waikato stores rows like "Bachelor of Business (BBus) All major subjects: Accounting, Agribusiness, …". `token_sort_ratio("Bachelor of Business", long_name)` scores only 14 (miss). The fix: strip " All major|major subjects: …" via regex at parse time → stored pattern becomes "Bachelor of Business (BBus)" → score 85 (high confidence match).

Applied in `_parse_fee_page_html` immediately after reading `prog_name`, before Pattern A/B processing.

**Why:** token_sort_ratio sorts both strings' tokens alphabetically and compares. Long subject lists add dozens of tokens that swamp the short course name tokens.

**How to apply:** Other universities with similar verbose fee tables benefit automatically. The regex only strips after "All {qualifier} subjects?:" so short parenthetical abbreviations like "(BBus)" are preserved.
