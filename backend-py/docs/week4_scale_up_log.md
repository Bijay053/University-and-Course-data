# Week 4 Scale-Up Log — Top 10 AU Universities

**Status legend:** APPROVED · DEFERRED · IN_PROGRESS · BLOCKED · NOT_STARTED

| # | University | Slug | YAML | Run ID | Discovered / Staged / Skipped | Critical / Warn / Info | Spot-checks | Cost / Run | Avg / Course | Decision |
|---|------------|------|------|--------|-------------------------------|------------------------|-------------|------------|--------------|----------|
| 1 | Monash University | monash | stub | — | — | — | — | — | — | NOT_STARTED |
| 2 | University of Melbourne | unimelb | stub | — | — | — | — | — | — | NOT_STARTED |
| 3 | University of Sydney | usyd | stub | — | — | — | — | — | — | NOT_STARTED |
| 4 | UNSW | unsw | stub | — | — | — | — | — | — | NOT_STARTED |
| 5 | University of Queensland | uq | stub | — | — | — | — | — | — | NOT_STARTED |
| 6 | RMIT | rmit | stub | — | — | — | — | — | — | NOT_STARTED |
| 7 | Deakin University | deakin | stub | — | — | — | — | — | — | NOT_STARTED |
| 8 | UTS | uts | stub | — | — | — | — | — | — | NOT_STARTED |
| 9 | ANU | anu | stub | — | — | — | — | — | — | NOT_STARTED |
| 10 | UWA | uwa | stub | — | — | — | — | — | — | NOT_STARTED |

---

## Suggested scrape order (Prompt 2 readiness)

1. **Group A — lowest risk** (start here): unis on Adobe Experience Manager / standard
   server-rendered HTML.  ANU, UWA, UQ, Sydney are typically in this bucket.
2. **Group B — medium**: unis behind Cloudflare or with custom CMS.  Monash, Melbourne,
   UNSW, UTS.  Likely need `skip_static_fetch: true` and a longer browser timeout.
3. **Group C — heaviest JS**: RMIT, Deakin.  Expect to need `browser_timeout_seconds: 90`
   and possibly per-uni `extra_discovery_seeds`.

Order finalises after Prompt-2 spot-check of one course page per uni from a browser
(open the homepage, click "Find a course", capture the discovery URL pattern, note
whether the listing renders without JS).

---

## Per-uni run template

Copy this block into a new `## <Uni Name> — <date>` section per scrape attempt.

```markdown
## <Uni Name> — YYYY-MM-DD

- **Run ID**: <runtime_job_id>
- **Discovered / Staged / Skipped**: N / N / N
- **Skip breakdown**: domestic=N online=N no_int_fee=N category=N fetch_failed=N
- **Alerts**: critical=N warning=N info=N
- **Spot-checks** (2-3 courses, picked from RANDOM() query in Prompt 3 Step 4):
  - Course A — `<source_url>`
    - course_name: ✅ / ❌ <observed vs source>
    - international_fee: ✅ / ❌
    - fee_term: ✅ / ❌
    - ielts_overall: ✅ / ❌
    - duration: ✅ / ❌
    - intake_text: ✅ / ❌
    - course_location: ✅ / ❌
    - study_mode: ✅ / ❌
    - cricos_code: ✅ / ❌ / N/A
  - Course B — ...
  - Course C — ...
- **Cost**: $X.XX | **Duration**: Xm Xs | **Avg cost/course**: $X.XXXX
- **Decision**: APPROVED / DEFERRED — <reason>
- **Notes**: <anything unusual: WAF blocks, JS-rendered fees, fee_term parsed wrong, etc.>
- **YAML changes (if DEFERRED)**: <list keys added to per-uni YAML>
```

---

## Cumulative metrics

After every 2 unis, copy the result of `backend-py/scripts/week4_cost_projection.sql`
here so the running cost trend is visible across the week.

```text
date            unis_done   avg_cost_per_uni    projected_80_uni_per_scrape   notes
YYYY-MM-DD      2/10        $X.XX               $XX.XX                        ...
YYYY-MM-DD      4/10        $X.XX               $XX.XX                        ...
```
