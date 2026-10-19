# Northumbria fresh catalogue verification — 19 October 2026

## Verdict

**The fresh, review-only run completed safely, but full catalogue coverage is
not verified.** It staged **310 of 361 current full-time, non-distance
catalogue candidates (85.9%)**. The remaining **51** comprise **8 URL-filter
exclusions, 40 extraction timeouts, and 3 unjustified course-page exclusions**.
This is a URL/offering coverage measure, not an approval of international
eligibility or extracted field accuracy.

The **386 existing published course rows and 61 pre-existing pending rows
were unchanged**. All **385 newly staged rows remain pending**; none was
automatically published. Of these, **75 are not in the current main course
finder** and need separate currency/eligibility review.

## Exact run and safety

- Production university: **96**, Northumbria University.
- Job: **`job_8946508ca070`**.
- Worker/release revision: **`f9d8af9965344ec4a6f6921cc799a9b3c80d1df6`**.
- Started **04:14:26 UTC**; completed **05:04:47 UTC**, approximately 50m 21s.
- Production API and worker were healthy. Database active jobs and worker
  active/reserved/scheduled deliveries were empty; these checks were repeated
  before dispatch. The existing worker had started at 03:49:57 UTC.
- Started once through the supported `app.routers.scrape.start_scrape` handler:
  `universityId=96`, root discovery URL, `fullCatalogueReviewOnly=true`,
  `forceDiscovery=true`, `fastMode=false`. It returned a new job, not a reused
  one. No selected URLs, checkpoint resume, or bounded verification sample
  were substituted for full discovery.
- No production deployment, settings change, worker restart, lock clearing,
  stop request, approval, deletion, or manual redispatch was performed.
- Final policy persisted `automatic_publish=false`, `automatic_repair=false`,
  `preserve_existing=true`, `removal_reconciliation=false`,
  `saved_checkpoints=false`, and `full_catalogue_verified=false`.
- Final database inspection found no active Northumbria job and no derived
  jobs referring to this run. It was claimed once and never requeued.

Full ordered-row JSON fingerprints matched before and after:

| Protected set | Rows | MD5 before = after |
|---|---:|---|
| Published `courses`, university 96 | 386 | `4a162f72f25c45004c56c048557cc404` |
| Pre-run pending `scraped_courses` | 61 | `998ec1db2a5f694e5e561b982c072327` |

These hashes cover the full parent rows, not every dependent-table row.
New staging statuses are **383 pending/review** and **2
pending/data_quality_failure**. “Imported” in this job means staged, not
published.

## Effective configuration and stale-cache check

The production `recipe` was null. The effective administrator override was:

```text
allow:
  ^https?://www\.northumbria\.ac\.uk/study-at-northumbria/courses/[^/]+/$
block:
  /study-at-northumbria/courses/.+(archive|course-finder|results)/
  /study-at-northumbria/courses/.*(contact|apply|enquire)
  /study-at-northumbria/courses/.*(tutor-profiles|study-and-travel-abroad)
  /sitemap\.xml
```

The old auto-generated configuration still contained the defective broad
course block, but the effective admin override replaced it. The effective
settings retained the configured sitemap, `always_sitemap_supplement=true`,
no `expected_min_courses`, no additional allowed hosts, and no
`course_detail_url_patterns`.

The old seven-link cache had already been replaced by later runs. It was
**not** reinserted or deleted for this test. Passing those same seven
navigation links to the **production** `reusable_course_count` and coverage
gate returned **0 reusable courses / insufficient coverage**. Legacy entries
without current scope metadata are also ineligible under the existing scope
gate.

For this actual run, the worker journal explicitly recorded:

```text
04:14:27 [DISCOVER] forceDiscovery=true — bypassing discovery URL cache
```

Durable discovery events then recorded live BFS and sitemap processing. This
was not a cached seven-link result.

## Official denominator and discovery

Official sources fetched directly with HTTP 200:

- [Current course finder](https://www.northumbria.ac.uk/study-at-northumbria/courses/):
  displayed **406 courses found**, matching **406 distinct course links** in
  its offering cards, including grouped duration/start-date alternatives.
- [Official sitemap](https://www.northumbria.ac.uk/sitemap.xml):
  **13,471 distinct URLs**, including **3,237 course-namespace URLs** and
  **532 single-segment course-detail URLs**. Child pages and unlisted courses
  make the sitemap unsuitable as a current eligible-course denominator.

The finder contains **363 full-time-labelled routes**, including **2 explicitly
distance-learning routes**, plus **37 part-time routes** and **6 other
distance-learning routes**. Removing the 37 part-time and all 8 distance
routes leaves **361 full-time, non-distance candidates**. Their international
eligibility and degree identity still require normal evidence checks.

Observed fresh discovery:

| Step | URLs |
|---|---:|
| BFS: 406 course links plus 7 navigation links | 413 |
| Sitemap detail candidates | 532 |
| Sitemap additions not already in BFS | 183 |
| Combined discovery | 596 |
| Legitimate navigation exclusions | 7 |
| Incorrect single-segment-whitelist exclusions | 8 |
| Sent through extraction | 581 |

All **406 current finder URLs** are accounted for in this run's URL outcomes
or its explicit filter-drop list. **398** passed to extraction; the other
**8** were filtered out. This confirms discovery coverage of the finder,
**not successful extraction or full catalogue completeness**.

## Reconciled results

The runtime row's counters alone omit fetch failures. The terminal event and
URL-level staging/error events reconcile the full work list:

**581 = 385 staged + 96 skipped + 82 timeouts + 18 fetch failures.**

| Cohort | Pending | Skipped | Timeouts | Fetch failures | Filtered before extraction |
|---|---:|---:|---:|---:|---:|
| Current finder, 406 URLs | 310 | 41 | 47 | 0 | 8 |
| Sitemap-only additions, 183 URLs | 75 | 55 | 35 | 18 | 0 |
| Current full-time/non-distance candidates, 361 URLs* | 310 | 3 | 40 | 0 | 8 |

*The final row is a subset of the first, not an additional cohort.*

The original Accounting and Finance MSc URL
`/courses/msc-accounting-and-finance-dtfaaf6/` was freshly extracted and staged
as pending row **52854**. The existing pending review set was preserved.
Its presence does not certify its individual extracted values.

There are only **578 distinct durable extraction-progress log URLs**, but
the terminal per-URL outcomes account for **all 581**. Three missing progress
entries are identified in the receipt. No missing progress log was treated
as proof of missing extraction or silently ignored.

## Exclusion audit and unresolved gaps

### Eight current courses incorrectly filtered

All eight are full-time offerings in the official finder. Direct, bounded
HTTP checks returned real HTTP 200 course pages:

1. Global Business Management (Human Resources) Top-Up, London.
2. Global Business Management (Logistics & Supply Chain) Top-Up, London.
3. Global Business Management (Marketing) Top-Up, London.
4. Global Business Management Top-Up, London.
5. International Banking and Finance Top-Up, London.
6. International Tourism, Hospitality and Events Top-Up, London.
7. Mechanical Engineering BEng (Hons), i2i International Soccer Academy.
8. Sustainable Civil Engineering MSc.

The first six redirect to official `london.northumbria.ac.uk` course pages.
The last two remain on the main host. Their nested `/undergraduate/` or
`/postgraduate/` paths fail the effective single-segment allow rule. Exact
source URLs, final URLs, H1s, response hashes and timestamps are retained in
the nested-route evidence. A future fix must preserve official-host checks
and course identity rather than broadly admitting every descendant page.

### Three unjustified exclusions after extraction

- **Criminology BSc (Hons)** and **Criminology (Extended Degree) BSc (Hons)**:
  rejected as `online_only`. Current course-owned facts publish full-time
  study, Newcastle/City Campus context, and international tuition. For the
  extended degree, the rule extractor recorded `Online` while visible online
  references concerned the shop and quizzes alongside lectures/seminars.
- **Psychology with Criminology BSc (Hons)**: rejected as
  `category_landing_page_missing_degree_qualifier`. Its H1 omits the award,
  but the document title and official finder name the BSc (Hons); structured
  facts provide full-time duration, UCAS code, start date and international
  tuition. This is not merely a category page.

### Valid exclusions versus incorrect labels

Of the **41 current-finder staging skips**, **38 have independent exclusion
evidence**: 30 are part-time offerings, 6 are distance-learning offerings,
and the full-time English Literature MA and History MA routes explicitly
identify **Distance Learning** in their route/H1. Full-time study load does
not make those two campus-delivered courses.

Some pipeline labels are imprecise: the part-time EMBA and musculoskeletal
course were called category pages, and the part-time Bar Course LLM was called
online-only. Their exclusion is nevertheless supported independently by
the listing's part-time route. These labels should not be treated as verified
page classifications.

The **55 sitemap-only skips** comprise 23 part-time, 16 online-only and 16
missing-degree-qualifier decisions. They were **not each independently
validated**, so this report does not certify all 96 exclusions as correct.

### Remaining failures and review quality

- **40 current full-time/non-distance candidates timed out** at the existing
  90-second extraction deadline. Another 7 current part-time routes timed out.
  These are failures, not evidence that the courses are ineligible.
- The other **35 timeouts and 18 fetch failures** belong to sitemap-only URLs.
- The terminal event records **zero sweep time**. The isolated review path
  suppresses the ordinary recovery sweep; earlier error messages saying
  “sequential recovery sweep” do not prove a retry happened.
- **75 pending rows are absent from the current main finder**. They may be
  legitimate alternative-campus/older routes, but current availability was
  not established individually. Do not bulk-approve them on count alone.
- Two new rows have critical quality holds: the Professional Legal Practice
  LLM has a suspicious **£2,006 annual fee**, and the MBA/MPA page lacks an
  English requirement. Both remain pending/data-quality-failure.

## Budgets and validation

- Existing configured limits were unchanged: **12 parallel extraction slots**,
  **90 seconds per course**, **$1 recorded Gemini-primary budget**.
- Recorded Gemini-primary cost: **$0.08513677**, ceiling not hit.
- Recorded Scrape.do static/render calls: **0 / 0**.
- Runtime diagnostics include **111 Gemini timeouts** and **217 circuit-open
  skips**. These are provider/gate counters, not additional course failures.
- The cost figure excludes other AI/vision/fallback and transport costs; it is
  **not a total provider-spend claim**.
- Additional source checks were bounded direct HTTP requests, concurrency at
  most 2, with no paid proxy and no separate production scrape/retry.
- Existing cache-scope and full-review-policy tests: **42 passed**.
- The development login page rendered and FastAPI started cleanly. The
  authenticated review UI was **not visually verified**; production proof here
  comes from the durable job, logs, staging rows and before/after fingerprints.

## Evidence

- `northumbria_verification_2026-10-19.json`: compact receipt, complete
  current-finder URL/outcome ledger, terminal counters, policy, filters,
  source hashes and exclusion traces.
- `northumbria_preflight_2026-10-19.json`: pre-dispatch safety, baseline hashes,
  supported request and production navigation-cache negative test.
- `northumbria_nested_routes_2026-10-19.json`: eight excluded course probes.
- `northumbria_criminology_delivery_2026-10-19.json` and
  `northumbria_exclusions_2026-10-19.json`: independent delivery/degree evidence.

Verbose generated query dumps are kept outside Git/deployment in
`generated-artifacts/northumbria-catalogue/`. The production job and
`scrape_runtime_logs` remain the durable source for replaying the audit.
No runtime scraper code was changed as part of this verification.
