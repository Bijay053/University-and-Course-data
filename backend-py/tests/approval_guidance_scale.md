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
- Opt-in summary: under 5 seconds, at most six queries, under 20% of
  the equivalent full-response bytes; per-row evidence counts equal full
  evidence lengths without reading snippets.
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

## Opt-in summary and fenced evidence hydration

Both `GET /api/scrape/staged?view=summary` and
`GET /api/scrape/staged/{jobId}?view=summary` keep their existing list/wrapper
shapes and ordering. Omitted `view` (or `view=full`) is unchanged. Summary
rows omit source `evidence`, `extraction_method`/`extractionMethod`, and
`raw_data`/`rawData`; a small `extractionMethod.ulaw_qualification_scope`
marker retains only boolean truthiness (`null` and `false` remain distinct)
where present, never nested `pre_split` or `verified_source` proofs, for the
qualification-refresh list action. Legacy authoritative
`extraction_method.fee_variants` is promoted to top-level `feeVariants` before
stripping metadata, so the fee table and fee-protected actions retain their
source authority (including the selected fee option's source URL and snippet).
For uniform fees the minimal `extractionMethod.campus_fee_scope.locations` is
also retained, along with positive integer `split_from_id` and nonempty
`original_name`, without copying scope source diagnostics or any other
extraction metadata. Fee display uses the locations; explicit sibling
identities keep legacy campus rows grouped. Existing top-level `feeVariants`
takes precedence over the legacy metadata value.
They retain public statuses, guidance, fee selection, editable course fields,
and action IDs, and add `evidenceLoaded: false` and `evidenceCount` (one grouped
count query; no snippet retrieval). Hydrate a selected row with
`GET /api/scrape/staged/{sc_id}/evidence?jobId=...&universityId=...`.
Both fences are required (422 if absent); mismatches return 404. The endpoint
requires `staged.view` and returns `{ "course": <full staged row> }` including
source evidence and editable fields, using the same public guidance redaction
and inherited-score suppression as the staged list. It does not return internal
qualification fingerprints or legacy diagnostic containers.
Numeric `GET /staged/{id}?view=summary` remains a full single-row response:
the opt-in projection applies only to the two list routes, not single-row
lookups.

History Review has the same opt-in source loading contract:
`GET /api/scrape/history/{runtimeJobId}?view=summary` preserves `job`, `logs`,
`provider_failure`, `unresolvedCourses`, and the order and scalar fields of
`stagedCourses`, but omits each row's `evidence` and adds `evidenceLoaded:false`
and grouped `evidenceCount`. Omitting `view` (or specifying `view=full`) keeps
the previous full history response exactly; non-existent jobs return 404, and
the staged rows are constrained to the requested runtime job. History's
existing access-control contract is unchanged.

Measured locally on the same rollback-owned 241-row synthetic fixture (one
passing run; ASGI HTTP request including database/JSON encoding, 2026-09-27):

| Operation | Seconds | Queries | HTTP bytes |
|---|---:|---:|---:|
| Full 100-row page | 0.607 | 5 | 19,034,039 |
| Summary 100-row page | 0.263 | 5 | 427,247 |
| Full 241-row job review | 1.050 | 5 | 45,702,612 |
| Summary 241-row job review | 0.313 | 5 | 1,034,722 |

These are local observations, not production SLAs; the summary page is 97.8%
smaller and the summary job review 97.7% smaller than their full counterparts.
The test also checks exact default-versus-explicit-full equality, row identity
and ordering, six/zero evidence counts as applicable, full hydration equality,
redaction, permission denial, both required fences, and mismatched fences.

The history extension was measured separately in the same 241-row synthetic
fixture (one passing run, 2026-09-27):

| Operation | Seconds | Queries | HTTP bytes |
|---|---:|---:|---:|
| Full history | 1.793 | 6 | 2,015,070 |
| Summary history | 0.328 | 6 | 191,107 |

Summary history was 90.5% smaller. Regression checks also compare the
default and explicit full payloads, identical non-course metadata, exact
ordered row IDs and public scalar values, evidence counts, a second
university/job exclusion fence, and invalid/missing-job handling.