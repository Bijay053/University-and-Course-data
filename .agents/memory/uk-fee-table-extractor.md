---
name: UK structured fee table extractor
description: Pre-pass 0 in fee.py that parses Home/International×Full/Part-time fee tables before text scanning.
---

# UK Structured Fee Table Extractor

## Rule
`_extract_fee_table_row(html)` runs as **Pre-pass 0** in `fee.extract()`, before the strong-label structural extractor and the flat-text keyword scanner.

**Why:** UK universities (Wolverhampton, Coventry, etc.) publish multi-row fee tables. The flat-text scanner has no row-boundary awareness and picks Home-student fees instead of International fees.

## How to apply
- Returns `(amount, ctx_str)` → use as fee; confidence 0.92
- Returns `_FEE_TABLE_FOUND_NO_INTL` sentinel → return `[]` immediately (no fee stored; suppresses text-scan fallback picking up a Home/part-time amount)
- Returns `None` → no structured fee table; fall through to Pre-pass 1 (strong label) then keyword scan

## Sentinel
`_FEE_TABLE_FOUND_NO_INTL = object()` — always compare with `is`, not `==`.

## Detection criterion
A table qualifies as a fee table when ≥1 row contains ALL THREE:
- (Home OR International) AND (Full time OR Part time) AND a currency amount

## Row selection
- Must have "International" (or "Overseas") AND "Full time" (no "Part time" in same row)
- Among qualifying rows, picks the **latest year** (first `20XX` in row text)
- Ties broken by larger amount

## Part-time-only courses
If a fee table is detected but has NO International + Full-time rows, the extractor signals `_FEE_TABLE_FOUND_NO_INTL` so `international_fee` is left blank. Data quality will flag these for operator review (e.g. HNC Building Studies — part-time-only course, should be manually rejected).
