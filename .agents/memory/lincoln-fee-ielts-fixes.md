---
name: Lincoln University fee matching + IELTS defaults
description: How to fix fee coverage for NZ universities with bracket/honours variants + use degree_level_defaults for IELTS when central page is Cloudflare-blocked.
---

## Fee matching s3b/s5/s6 in central_pages.py `_score()`

Three additional scoring strategies added to `match_central_fee()._score()` for universities with complex course-name variants in fee tables:

**s5 — paren-normalized token_sort_ratio**
- Problem: "(Finance)" is ONE token → token_sort_ratio treats it differently from "Finance"
- Fix: strip parens + extra spaces → `_p_pnorm` and `_n_pnorm`, run token_sort_ratio on normalized versions
- Catches: "Master of Business (Finance)" ↔ "Master of Business in Finance"

**s6 — "with Honours" strip, require ≥90**
- Problem: "Bachelor of Science with Honours" matches "Bachelor of Science" poorly (adds tokens)
- Fix: strip "with Honours" / "with First Class Honours" from course name, re-score with partial_ratio; only accept if score ≥ 90 (prevents over-broad match)
- Catches: "BSc with Honours" ↔ "BSc" in fee table

**s3b — reverse-startswith guard**
- Problem: s3 is fee-pattern startswith course-name; s3b is course-name startswith fee-pattern
- Use case: "Master of Science (Research)" ↔ "Master of Science" row — n.startswith(p) is True
- Guard: only run partial_ratio if nothing after p, or next char is space/paren

Final: `return max(s1, s2, s3, s4, s5, s6, s3b)`

**Why:** rapidfuzz's default token_sort_ratio normalises away important suffix distinctions; these three strategies give bracket/honours/suffix variants a fair shot without lowering the global threshold.

## IELTS defaults via degree_level_defaults (EnglishConfig)

`extraction.english.degree_level_defaults` in per-uni YAML is already fully wired in the pipeline (`single_course.py` line ~5650). Applied as LAST resort (confidence 0.40) when ALL earlier extractors (per-course HTML, browser, vision, central page) return null.

```yaml
extraction:
  english:
    default_ielts: 6.0        # fallback for unmapped degree tiers
    degree_level_defaults:
      undergraduate:
        ielts: 6.0
      postgraduate:
        ielts: 6.5
      doctorate:
        ielts: 6.5
```

**When to use:** University's entry requirements page is Cloudflare-protected AND the rendered text has no parseable IELTS patterns (lincoln.ac.nz /study/entry-requirements as of 2026-06-03).

**Why it works:** confidence 0.40 < central_page 0.50 → a real central page always wins; per-course proven evidence also wins.

**Degree tier mapping (in single_course.py):**
- "bachelor" / "honours" / "honor" → undergraduate
- "master" → postgraduate
- "doctor" / "phd" / "dphil" → doctorate
- startswith("graduate") or "postgraduate" in name → postgraduate
- "diploma" / "certificate" (no graduate prefix) → undergraduate
- doctorate falls back to postgraduate if no doctorate key

## Lincoln University results (2026-06-03)

Fee: 69.5% → 89.8% | IELTS: 3.4% → 100% | Ready≥85%: ~64% → 91.5% | Avg: ~76% → 91.8%

5 remaining sub-85%: PhD (per-credit fee genuinely unmatchable), 4 PG Cert/Diploma courses (study_mode + academic_level missing from Gemini — per-run variability).

## scrape_runtime_jobs schema

PK column is `runtime_job_id` (NOT `id` or `job_id`). FK from scraped_courses is `scrape_job_id` which is the string token like `"job_abc123"`.
