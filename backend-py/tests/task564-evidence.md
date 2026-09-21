# Task 564 acceptance harness

## Observed execution — PASS

The owning agent executed v5 successfully on **2026-09-21 UTC**. The completed
evidence artifact timestamp was **13:32:18.638155 UTC**. This document records
that actual run, not a new execution.

**Final cleanup verified:** v5 includes generated recipe isolation and complete
production recipe-tree hash comparison, closing the filesystem gap discovered
after v4. The retained JSON records this final v5 pass.

```sh
python backend-py/tests/task564_acceptance.py --output /tmp/task564-evidence-v5
```

Sanitized durable results:
[`evidence/course-report-worker-acceptance.json`](evidence/course-report-worker-acceptance.json).
No browser session traces, authentication headers, credentials, or raw server
logs have been copied into that artifact.

| Observed check | Explicit course URLs | 55-link catalogue |
| --- | --- | --- |
| Worker status | completed | completed |
| Found / processed / staged | 2 / 2 / 2 | 50 / 50 / 50 |
| Extraction errors | 0 | 0 |
| Audit status / phase | completed / needs_review | completed / needs_review |
| Worker dispatch attempts / verification jobs | 1 / 1 | 1 / 1 |
| Observed Gemini-primary cost | $0 | $0 |
| Exact child row IDs | 3, 4 | 5–54 |
| Rendered review row IDs | 1, 3, 4 | 2, 5–54 |

The first report persisted **running** status across a fresh browser reload,
with independent database confirmation before releasing the HTTP fixture gate.
Both navigation checks retained the requested child summary and matched the
exact explicit-source-plus-child database row set. Every expected course name
and source URL rendered; child rows 3 and 5 opened their exact evidence dialogs.

Both existing source rows remained byte-for-byte identical under complete
PostgreSQL row JSON serialization. There were 52 newly pending rows (54 total),
zero published courses, and zero approved/published review rows. A 51-URL
request was rejected with HTTP 422.

The second fixture offered 55 links; production discovery selected, processed,
and staged exactly 50. Metadata recorded `limit_reached=true`, `capped=false`:
discovery stopped at the bound before final-list truncation. No URLs numbered
50–54 were requested. Both reports retained `full_catalogue_verified=false`.
The fixture received 69 requests across 57 paths.

Cleanup passed: PostgreSQL, Redis, API, frontend and fixture ports were closed;
all tracked subprocesses exited; the private data directory and recipe root were
removed. Generated `unis/task564_1.yaml` and `unis/task564_2.yaml` existed only
inside that private root and were removed with it. The complete production
recipe relative-path/SHA-256 maps matched before and after: no added, deleted
or content-changed files, and zero attempted production recipe writes.

## Reusable command

From the workspace root:

```sh
python backend-py/tests/task564_acceptance.py --output /tmp/task564-evidence
```

Requirements: PostgreSQL `initdb`/`pg_ctl`, Redis, pnpm, installed backend Python
dependencies, Python Playwright and its Chromium browser. It allocates random
loopback ports and starts dedicated subprocesses, not shared workflows.

## Isolation and fixture disclosure

The harness creates a fresh PostgreSQL cluster and Redis process. Database
credentials and all server destinations are generated locally. It does not use
the ambient DATABASE_URL or Redis settings. Children receive an environment
allowlist rather than inherited service credentials. Schema comes from SQLAlchemy
models plus the production worker-claim migrations.

`task564.example.test` is a reserved synthetic university, not a real university
source. Only this host's safety check and HTTP transport destination are
substituted in test subprocesses. A real local HTTP server provides course HTML.
No dispatch, Celery task, router, authentication, extractor, staging function,
workflow monitor, or frontend API response is mocked. This does not prove live
university coverage or live DNS/SSRF behavior.

## Assertions and evidence

The browser signs in through the real login screen, opens the real Scraping page,
submits its CourseReport form, waits for durable running state, reloads and
asserts both rendered and database status are still running before releasing
the fixture gate, then uses the report's review action. The exact
`/api/scrape/staged/{child_id}` response's row IDs are compared to the exact
child-plus-explicit-source database set, matching the production continuation
review contract. The response summary must retain the requested child ID; every
row's own job identity must match the database. Unrelated rows are excluded.
Every expected course name and official URL must render in its review table row;
opening one row's evidence must request that exact database row ID and show a
dialog for a newly recovered child row. Real worker completion must stage at least one pending review row, and
the real queued monitor must finalize an audit.

Source rows are compared using PostgreSQL's complete row JSON serialization
before and after, including all fields and timestamps. Both published courses
and approved/published review rows must remain zero.

The test checks the durable 50-course / 600-second / $2 policy, observed processed
count and cost, audit dispatch/run counts, and server rejection of 51 URLs.
A second university/source report discovers a catalogue offering 55 degree links
and must persist exactly 50 selected URLs with `limit_reached=true`, stage no more
than 50 rows, preserve both sources, and navigate to that exact child's review.
This forces the course-count boundary. It does not force timeout, cost-cap
exhaustion, or repeated worker-crash recovery. The $2 setting covers observed
Gemini-primary extraction costs only, not a hard total-provider-spend cap;
metadata explicitly records `total_cost_cap_enforced=false`. With no AI
credentials, the observed zero cost does not test paid-provider exhaustion.
The UI limit warning checks only `capped`; this run reached the earlier discovery
bound with `limit_reached=true` and `capped=false`, so warning rendering for that
case is not claimed.

The output directory contains `evidence.json`, a Playwright trace, screenshots,
and dedicated subprocess logs. The JSON records failure diagnostics as well as
success and verifies cleanup: every private listening port closed, every tracked
process exited, and the private data directory removed. Keep these actual run
artifacts locally for diagnosis. Browser traces may contain session credentials
and are intentionally not checked in. The sanitized durable summary is retained
alongside this document instead of credential-bearing raw artifacts.

## Harness-only correction history

These failed attempts were test setup/assertion corrections, not accepted passes
or production changes:

1. **v1:** POST failed because ORM-created `ai_repair_audits` lacked migration
   server timestamp defaults used by raw workflow SQL. Harness now creates it
   using actual migration 047. Cleanup passed.
2. **v2:** Worker staged both courses, but `asyncio.run` collided with sync
   Playwright's running event loop. Snapshots now execute in an independent
   thread. Missing taxonomy schema was also corrected by running actual
   migration 040, including canonical seed rows.
3. **v3:** Real workflow completed, but an overstrict child-only assertion
   rejected the intentionally included explicit source row. Trace inspection
   confirmed the documented `retrySourceJobId` continuation review contract.
   Assertions now require the exact two-job row union, verify each row's job
   identity, exclude unrelated rows, and still require exact child navigation.
4. **v4:** Both browser/worker scenarios and process/port cleanup passed, but
   post-run inspection found two fixture YAML stubs in the production recipe
   tree. Only those generated stubs were removed. The harness redirected
   generated/runtime recipe paths to its temporary root, added a read-only
   production-tree guard, and asserted before/after file hash equality.
5. **v5:** Both scenarios passed again, including private recipe removal,
   unchanged production file hashes, and zero attempted production writes.

The fixture hostname was also corrected from `task564.example.edu` to the
unambiguously reserved `task564.example.test` before the successful run.