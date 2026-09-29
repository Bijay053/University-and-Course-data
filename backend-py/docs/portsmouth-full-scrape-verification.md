# Portsmouth full-scrape verification — 29 September 2026

## Scope and provenance

The fresh **development** scrape used the ordinary queued worker path, university ID
`2174`, job `job_c93c87eb4d79`, `forceDiscovery=true`, `fastMode=false`, and the
Portsmouth configuration's `max_parallel_fetch: 2`. It did not approve or publish
courses. The comparison run is the earlier attached Portsmouth worker log
(`Pasted-Worker-claimed-queued-scrape-job-job-id-job-8b426874aed_1790668696276.txt`).
Both runs reported 506 raw candidates and 435 extractable course URLs. Counts below
come from `scrape_runtime_jobs`, `scrape_runtime_logs`, `scraped_courses`, and the
attached log; the comparison log stops after its quality summary rather than showing
a terminal job status.

| Measure | Earlier worker log | Fresh two-slot run |
| --- | ---: | ---: |
| Raw / extractable URLs | 506 / 435 | 506 / 435 |
| Per-course extraction failures | 163 (`per_course_timeout`) | 0 |
| Recovery | 26/163 recovered, 27 attempted, 136 remaining when budget expired | No sweep needed |
| Staged for review | 83 checked by quality pass (58 initially saved, 25 sweep-saved) | 302 |
| Online-only staging exclusions | 213 | 92 |
| Part-time-only staging exclusions | 1 | 41 |
| Other staging exclusions | 0 in the attached log | 0 |
| Quality findings | 6 critical / 57 warnings / 46 info on 83 checked | 0 critical / 244 warnings / 145 info on 302 checked |

The fresh job completed normally: `status=completed`, `total_found=435`,
`current=435`, `imported=302`, `skipped=133`, `errors=0`. Its gate counter reconciles
exactly: 92 online-only + 41 part-time-only = 133 skips; 302 + 133 = 435 URLs.
There were no `[COURSE TIMEOUT]` or `[STAGE] extraction failed` events and no
recovery sweep. All 302 rows from this job are `status=pending`,
`auto_publish_status=review`; no row from this run was approved or published.

The job ran from 08:18:51 to 09:02:12 UTC, **43 minutes 20 seconds**. This is
inside the currently configured 120-minute Celery soft limit, but is not a quick
scrape. The earlier attachment has no end-to-end timestamps, so this evidence
establishes complete, timeout-free coverage at two slots, **not** that the new
setting improved wall-clock speed. Revisit the latency trade-off if a shorter
operational target is required.

## Fee review exceptions

The improvement in extraction coverage does not certify fee correctness. The fresh
quality pass logged 27 annual-low-fee warnings and no critical fees, versus six
critical fees in the smaller earlier checked set. It also logged 191
`missing_international_fee` warnings, although the final staged rows contain only
16 null international fees. The differing snapshot and final-row counts need
investigation; neither number should be presented as the other.

Two prices in the final staged rows conflict with the live official audience rows:

- **MA Animation** (`/study/courses/postgraduate-taught/ma-animation`): stored
  £9,700 annual, selected from a generic regex snippet starting with the UK
  tuition section. The official page lists £9,700 for UK and £18,600 for
  International and EU full-time students.
- **BA Animation** (`/study/courses/undergraduate/ba-hons-animation`): stored
  £10,300 annual, carried forward from a previously approved row. The current
  page lists £10,300 for EU students and £17,900 for international students.
  Its current-run Gemini candidate was £17,900 but was not selected.

These are confirmed review risks, not grounds to bulk-rewrite approved records.
The live MSc Accounting and Finance page and selected evidence both show the
correct £18,600 international full-time fee, illustrating that the course-owned
accordion path works where that template is present. A separate follow-up
should handle plain fee sections and inherited EU/UK values before reviewers
trust the entire fee cohort.