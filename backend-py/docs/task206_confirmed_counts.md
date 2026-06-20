# Task #206 — Confirmed Course Counts: Portsmouth & Ulster

**Date:** 2026-06-20  
**Environment:** dev (Replit)

## Portsmouth (uni_id=2174)

| Metric | Value |
|---|---|
| Job ID | job_e7cb2e24bf4d |
| Status | completed |
| total_found | 421 |
| imported (staged) | **418** |
| errors | 0 |
| skipped | 3 |
| Threshold (≥250) | ✅ PASS |

All 418 staged rows have `status=pending` — ready for operator review/approve.

**YAML config active:** `backend-py/scraper_config/unis/port_2174.yaml`  
Discovery: `sitemap.xml?page=2` (explicitly configured) + `always_sitemap_supplement: true`.  
Staging flags: `skip_degree_qualifier_check: true`, `require_international_fee: false`.

---

## Ulster (uni_id=2176) — Batch 1 of 2

| Metric | Value |
|---|---|
| Job ID | job_22a384c2da4f |
| Status | completed |
| total_found | 461 |
| imported (staged) | **461** |
| errors | 0 |
| skipped | 0 |
| Threshold (≥350) | ✅ PASS |

Staging breakdown:
- `pending / data_quality_failure`: 399 courses — completeness below 85% floor.  
  Root cause: Ulster is Cloudflare-403 on every path; only Wayback Machine archives  
  are reachable for most course pages. Dec-2025 snapshots often lack international  
  fee tables → key fields empty → quality failure. See Task #213 for fix options.
- `pending / review`: 62 courses — staged and pending operator review.

**YAML config active:** `backend-py/scraper_config/unis/ulster_2176.yaml`  
Discovery: `sitemap-courses.xml` (987 raw URLs) → year_dedup → 461 staged (batch 1).  
Staging flags: `skip_degree_qualifier_check: true`, `require_international_fee: false`.

### Coverage gap

The sitemap has ~987 course URLs. Batch 1 (`max_candidates=500`, `sitemap_offset=0`)  
covered the first 500 sitemap entries (461 after year-dedup). Batch 2 covers the  
remaining ~487 URLs. To run Batch 2, set `sitemap_offset: 500` in the YAML, trigger  
a new scrape, then restore `sitemap_offset: 0` (or omit it) for future regular scrapes.

```yaml
# ulster_2176.yaml — Batch 2 settings (change then revert after the job completes)
discovery:
  max_candidates: 500
  sitemap_offset: 500   # skip first 500, pick up remainder
```

Expected Batch 2 output: ~487 sitemap URLs → ~430-470 after year-dedup and extraction.

### OOM root cause

First attempt (job_cb4deded6fd0, `max_candidates=1000`) loaded 961 courses into a  
single `asyncio.gather()` and died at ~50 min with heartbeat stale. Fix: `max_candidates`  
capped at 500 to match `_MAX_COURSES_PER_JOB`. Long-term fix: batch the gather() loop  
in the orchestrator (Task #215).

---

## Summary

| University | Staged | Threshold | Gap |
|---|---|---|---|
| Portsmouth (2174) | **418** | ≥250 ✅ | None — full catalogue |
| Ulster (2176) Batch 1 | **461** | ≥350 ✅ | ~487 remaining (Batch 2 pending) |
