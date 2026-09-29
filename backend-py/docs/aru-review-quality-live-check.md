# ARU Review quality live check — 29 September 2026

## Scope and method

The development university record for `https://www.aru.ac.uk` was present as
university `1752` (display name `University courses at ARU`). Redis and the
Celery scrape worker were started before a normal, non-fast scrape with fresh
discovery. The resulting job was `job_addaebe23f3c`. No staged courses were
approved or published as part of this check.

While extraction and staging were in progress, the job-scoped Review summary
was loaded repeatedly via `staged_one(job_id, db, view="summary")`, the same
`/api/scrape/staged/{job_id}?view=summary` response consumed by the Review
page's refresh action. This was a server-side invocation, not a signed-in
browser interaction: the browser preview of `/scraping` showed the sign-in
screen. The response includes each displayed `degreeLevel`, `courseName`,
and `courseQuality` calculated from that row snapshot.

## Observations

| Point in run | Review rows | Master rows | Master rows with `missing_degree_level` or an unfilled degree breakdown |
| --- | ---: | ---: | ---: |
| During staging | 43 | 40 | 0 |
| Later staging refresh | 92 | 70 | 0 |
| Further staging refresh | 164 | 72 | 0 |
| Completed job | 201 | 71 | 0 |

The Banking and Finance row (staged ID `226762`, displayed level `Master`)
appeared during staging and in the completed Review response. Its final
`courseQuality.score` was **100**, `courseQuality.issues` was empty, and the
degree-level breakdown had `fill: true`. It therefore had no “No Degree
Level” chip. The stored `degree_level` is `Master's`; the Review response
normalizes the display to `Master`. Other displayed Master rows, including
Accounting and Finance, Sociology, and Mechanical Engineering (Peterborough),
likewise had no missing-degree chip.

**Course identity limit:** The Banking and Finance row's source URL ends in
`/banking-and-finance-mba`, and its stored title does not include `MA`.
The deterministic regression test uses the title “MA Banking and Finance”,
but no row by that exact title appeared in the fresh job. This live check
verifies the current ARU Banking and Finance Master row and the other Master
rows; it does not claim to have seen an MA-titled Banking and Finance row.

On the completed response, all 201 Review rows had a `courseQuality.id`
matching the displayed row ID, `courseQuality.course_name` matching the
displayed course name, and a score equal to rescoring that same row with
`_score_quality_rows([course])`. There were zero score/row mismatches. The
worker reported **completed**, 340 discovered, 204 imported, and zero errors.
The 201 Review rows are the job-scoped pending rows after Review deduplication,
not a claim that every discovered URL became a visible Review row.

## Recheck

With the development database containing this job, call the Review summary
endpoint for `job_addaebe23f3c` or invoke `staged_one` with
`view="summary"` using an application database session. For every returned
course with `degreeLevel == "Master"`, assert that
`courseQuality.breakdown.degree_level.fill` is true and no issue has code
`missing_degree_level`. For every returned course, compare the quality
object's `id`, `course_name`, and `score` with the displayed row and a
single-row `_score_quality_rows([course])` result. This rechecks the completed
snapshot; to reproduce the race-sensitive staging observations, repeat those
reads during a new scrape.

The existing deterministic tests also passed:
`backend-py/tests/test_review_quality_snapshot.py` and the portal's
`review quality snapshot` refresh test in
`artifacts/university-portal/src/pages/scraping.test.tsx`.