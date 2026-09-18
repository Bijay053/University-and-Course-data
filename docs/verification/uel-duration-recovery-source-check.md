# UEL duration-recovery source check

## Scope and evidence

Read-only check of the completed isolated run `20260918T034045Z_b9e9ee`. I used
`duration-recovery.json`, the course-detail captures referenced by
`html-manifest.json`, and a read-only query to the disposable audit database.
No live page was fetched and no extractor verdict was treated as source
evidence.

The recovery arithmetic is internally consistent: 79 prior unique
duration-loss routes = 73 listed as staged + 6 listed as not staged.

## Six exclusions

| Route (short form) | Reported rejection | Course-owned evidence in captured HTML | Independent verdict |
|---|---|---|---|
| `edd-professional-doctorate-education` | `part_time_only` | The course-options card labels the home-applicant route **Part time**; no full-time route is shown. | **Supported** |
| `ma-special-additional-learning-needs-icep-europe` | `part_time_only` | The course-options card labels the home-applicant route **Part time, 2 years**; no full-time route is shown. | **Supported** |
| `msc-integrative-counselling-coaching` | `part_time_only` | The course-options card labels the home-applicant route **Part time**; no full-time route is shown. | **Supported** |
| `bsc-hons-policing-studies-top` | `part_time_only` | The course-options card labels the home-applicant route **Part time, 2 years**; no full-time route is shown. | **Supported** |
| `pgce-ipgce` | `online_only` | Although its option card says **Full time, 1 year**, the course-owned overview calls it a one-year **online** programme and the learning panel says it is delivered through an online platform plus school-based learning. | **Supported as online-only** |
| `pgcert-autism-spectrum-conditions-learning` | `part_time_only` | The course-owned **Course options** panel says **“No Course options available for this course.”** Its course JSON-LD has no course instance. The only part-time wording in the capture is a generic module-change note, not a mode declaration for this course. | **Not substantiated by the captured source** |

Thus, captured course-owned evidence supports **5 of the 6** exclusions (four
part-time-only and one online-only). The autism PGCert exclusion is
**unresolved, not disproved**: the capture supplies neither a current
part-time option nor a competing full-time option, so `part_time_only` cannot
be independently confirmed from this evidence.

## SEN route-count reconciliation

The isolated staging table contains both:

1. `pgce-primary-sen-inclusion` — “Primary with Sen (Inclusion) Pgce”
2. `pgce-primary-sen-special-schools` — the same name with a staged
   “(Special Schools)” suffix

However, both captured input routes identify the source course as **Primary
with SEN (Inclusion) PGCE**. Most decisively, the special-schools capture's
course-owned canonical link and `og:url` both point to
`pgce-primary-sen-inclusion`, and its option cards repeat the inclusion
course's full-time one-year routes.

Therefore the 369th staged row is the **special-schools input alias/variant of
the SEN Inclusion source course**, explaining `295 old + 73 recovered + 1
alias = 369 staged`. It is not evidence of an additional independently owned
UEL course page.

## Limit

This check verifies the six filtering decisions and the one-row SEN count
difference only. It does not re-audit unrelated catalogue fields or claim
that all 73 recovered duration values are otherwise correct.