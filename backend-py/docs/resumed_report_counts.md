# Resumed report count semantics

`imported` measures staged review records, not successful URL acknowledgements.
It is a scrape result count, not a count of currently pending approvals: an
existing same-child row remains counted even if a reviewer has approved or
rejected it. Recovery must preserve that row and its decision.

For a report child redelivered under the same runtime job ID:

- Count each saved same-child review row once.
- A `completed_urls` checkpoint means the URL was handled. It can represent a
  filter rejection with no saved row, so it must not increase `imported`.
- New filter acknowledgements also must not increase `imported`.
- Progress and attempted-work accounting may include settled URLs without rows;
  they are distinct from imported counts. Do not require `imported + skipped +
  errors == total_found` across a redelivery: some outcomes were settled earlier.
- Persisted worker counters, the final completion event, and fresh status/history
  projections must agree.

Ordinary catalogue resumes can preserve rows from an earlier runtime job.
Those matched cross-job review checkpoints still contribute to cumulative
imported totals. They are not the same as same-child delivery checkpoints, and
must not be lost when reconciling the current job's database row count.

## Cause of the inflated counts

Recovery combined staged-row URLs with `completed_urls`, then treated the size
of that union as already imported. The final database reconciliation also
replaced the invocation's staged count with all same-job rows and added the
resume offset again. This produced 5 (mixed) and 3 (fully resolved) against a
single preserved row.

The fix separates settled-work offsets from staged-row offsets and avoids
adding same-child rows again after reading the authoritative database count.
No preserved review records need to be edited or deleted to correct the count.

## Real-worker regression

Run from the repository root:

```sh
python backend-py/tests/task580_acceptance.py --output /tmp/resumed-report-counts
```

This uses production FastAPI routes, a real Celery prefork worker, the scraper
orchestrator, and disposable PostgreSQL/Redis. It waits for the Celery task's
post-run signal, restarts the API, and checks exact counts:

| Scenario | Saved child rows | Worker imported | Fresh status/history imported |
| --- | ---: | ---: | ---: |
| All-filtered retry | 0 | 0 | 0 |
| All-filtered report continuation | 0 | 0 | 0 |
| Mixed resumed continuation | 1 | 1 | 1 |
| Fully resolved redelivery | 1 | 1 | 1 |

The same assertions retain byte-for-byte comparisons of child rows, both earlier
review sets, approved records, and published courses. Evidence includes the
observed counts and checks that temporary services, data, and recipe files were
cleaned up. This is production-code-path testing in isolation, not a deployment
or mutation of the live database.

Validation on 2026-09-23: all four disposable scenarios passed with the exact
counts above and successful cleanup. Focused progress/catalogue-floor tests
passed (19), targeted-retry tests passed (21), and review-chain/stale-dedup
regressions passed (17). The latter require the project's asyncio auto mode;
running them with an empty pytest configuration instead uses strict mode and
fails fixture setup before exercising the tests.