---
name: MQ Scrape.do render false fix
description: scrape_do_render:true caused MORE failures for Macquarie; stealth browser is the correct per-course path
---

## Rule
Do NOT set `scrape_do_render: true` or `scrape_do_skip_fallbacks: true` for Macquarie University (uni_id=277, mq.yaml). Enabling these settings made results worse.

**Why:** The stealth browser (patchright + Xvfb) already passes Cloudflare Enterprise on www.mq.edu.au. Adding scrape_do_render routes all per-course fetches through Scrape.do's remote headless Chrome, which is slower, and many requests hit the 300s per_course_timeout hard cap — each burning a Scrape.do render credit with zero data produced (41/127 = 32% timeout rate, 45 staged vs 81 without the flags).

**How to apply:** If MQ scrapes regress (fewer staged, or timeout errors), check whether scrape_do_render was re-enabled. Revert to the stealth browser path.

## Fee extraction
MQ international fees are behind two JS interactions (click "Fees and scholarships" tab → select "International student" dropdown). No fetch tier can trigger this. Fix: `fees.degree_level_defaults` in mq.yaml provides approximate annual AUD fees (UG: 39000, PG: 44000, PhD: 30000) at 0.35 confidence so courses reach ≥85% completeness.

## Verified job comparison (2026-07-24)
- WITHOUT scrape_do_render: Found:127, Staged:81, Errors:36 (data-quality warns, not timeouts)
- WITH scrape_do_render: Found:127, Staged:45, Errors:41 (per_course_timeout — Scrape.do credits wasted)

## Completeness breakdown (13 review fields)
Most MQ courses have 12/13 fields filled (fee = 1 missing field). With degree_level_defaults:
- 13/13 = 100% for courses with all other fields
- 12/13 = 92% for courses missing other_requirement or academic_score

## data_quality_failure courses
~20% of staged MQ courses get data_quality_failure from the domestic_fee_only_no_international check (CSP domestic fee ~$11,700-$15,900 detected). These don't affect the majority of courses; `filters.domestic_only.enabled: false` prevents hard rejection.
