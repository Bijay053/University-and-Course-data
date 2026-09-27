# Durable approval guidance scale gate

Run from `backend-py`:

```sh
PYTHONPATH=. python -m pytest tests/test_approval_guidance_scale.py tests/test_durable_approval_guidance.py -q -s -c /dev/null -p no:cacheprovider
```

The scale test uses the existing rollback-owned PostgreSQL fixture (including
the real migration-defined invalidation trigger), synthetic universities/jobs,
240 large rows, and 1,440 source evidence records. It never fetches official
pages or calls a production endpoint. Fixture commits are savepoints inside an
outer transaction that is rolled back, including the test DDL.

Each row contains over 64 KiB of nested evidence, multiple fields/candidates,
source URLs, snippets, public proof hashes, and legacy private diagnostics.
Distinct creation times make pagination deterministic. HTTP timings include
database loading, fingerprint validation, redaction, and JSON encoding.
ORM objects are expired before every HTTP load.

## Fixed regression budgets

- Each 25/100-row page: under 5 seconds, exactly five application queries.
- Full-job Review reload (`/api/scrape/staged/{jobId}`), before and after
  guidance: all 241 expected rows, under 5 seconds, at most six queries with
  identical query counts and ordering, under 48.2 MB, and less than 300 bytes
  added per guided row.
- Response: under 200,000 bytes per requested row (existing evidence included).
- Guidance overhead: under 300 bytes per row relative to identical unguided data.
- Saving 240 guidance records: under 5 seconds.
- Each 240-row no-op/change/restore: under 5 seconds and exactly two application
  queries, excluding fixture savepoint statements.

These are deliberately broad local regression ceilings, not production SLAs.
The byte ceiling accommodates existing duplicated public aliases and large
source snippets; the separate incremental ceiling measures guidance itself.
The query assertions detect N+1 behavior independently of machine speed.

## Observed local results

Synthetic run on 2026-09-27:

| Operation | Seconds | Queries | HTTP bytes |
|---|---:|---:|---:|
| Unguided 100-row page | 0.814 | 5 | 19,034,039 |
| Save 240 guidance records | 0.250 | — | — |
| Guided 100-row page | 0.521 | 5 | 19,049,239 |
| Guided 25-row page | 0.464 | 5 | 4,762,576 |
| Final 41-row page | 0.406 | 5 | 7,809,706 |
| Bulk no-op | 0.064 | 2 | — |
| Bulk evidence edit | 0.048 | 2 | — |
| Bulk restore | 0.051 | 2 | — |

Guidance added 152 bytes per row without adding queries. No optimization or
production code change was justified. Single-run timings are not evidence that
guided reads are faster; warm-up and scheduling affect absolute durations.

Safety assertions cover exact row/job/university identities, unchanged control
guidance, no-op preservation, permanent invalidation after restore, pagination
membership, evidence retention, and nested private-diagnostic redaction.
The companion durable suite covers actual delayed failure writes and HTTP
permission/serializer boundaries.

The additional full-job route benchmark measured 1.665 seconds / 45,702,612
bytes before guidance and 1.125 seconds / 45,739,092 bytes after guidance,
with five queries in both cases. All 241 row identities were retained.