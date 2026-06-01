---
name: SCU year URL regex fix
description: Year extraction regex in Phase A.5c was incorrectly matching 4-digit course codes instead of actual years
---

## Rule
Phase A.5c `_YEAR_SEG_R` must use `20\d{2}` not `\d{4}` to match year segments in URLs.

**Why:** SCU (and similar AU unis) embed 4-digit course codes in URL paths (e.g. `admin-5350/2027/`). The original regex `[/_\-](\d{4})[/_\-\?]` matched `-5350/` (course code 5350, followed by `/`) BEFORE reaching the actual year `/2027/`. Since 5350 ∉ `[2027]`, those URLs bypassed the `ignore_years` filter and staged as duplicates.

**How to apply:** Any time the year regex is changed in orchestrator.py Phase A.5c, ensure it stays as `r"[/_\-](20\d{2})[/_\-\?]|[/_\-](20\d{2})$"`. The `20\d{2}` pattern restricts matching to years 2000–2099, so 4-digit course codes like 5350, 3007, 7001 are never mistaken for years.

## Also fixed
Recipe `block_url_patterns` (from the recipe UI editor) was not being applied during Phase A.5c. Added Step 0 in Phase A.5c to apply recipe-level block_url_patterns as substring deny-list before the ignore_urls_matching / ignore_years steps. This means recipes with `block_url_patterns: ["/2027/"]` now actually filter those URLs at discovery time.
