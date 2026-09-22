# Reviewed continuation worker acceptance

## Observed execution — PASS

The disposable acceptance harness passed on **2026-09-22 UTC**:

```sh
python backend-py/tests/task568_acceptance.py --output /tmp/task568-evidence
```

It created private PostgreSQL and Redis instances, started the production
FastAPI application and a real prefork Celery worker, and submitted 60 explicit
course URLs through the authenticated production API.

The first child used a harness-shortened 2-second stored deadline. Five URLs
settled and were durably checkpointed before the sixth synthetic page blocked.
The child finished `failed_degraded` with
`budget_exhausted=time_budget_exhausted`. After the real monitor moved the
report to `needs_review`, a fresh list request returned exactly URLs 5–59 as
remaining. The first worker was then stopped and a replacement prefork worker
was started. The two children were required to retain different worker PIDs.

Two simultaneous reviewed continuation requests produced one HTTP 202 and one
HTTP 409. A later stale request also returned 409. PostgreSQL contained exactly
one continuation child, linked to the timed-out child. Its bounded execution
slice was exactly URLs 5–54 and its independently stored policy remained 50
courses, 600 seconds, and $2. The child completed through the replacement
worker, staged exactly URLs 5–54, and left exactly URLs 55–59 for another
reviewed continuation.

The original source review row and every first-child review row were unchanged
after the second child. No published course was created. The continuation
contained none of the first child's settled URLs, and the fixture counters
confirmed that URLs 0–4 were not extracted again.

Cleanup removed the private database directory and generated scraper recipes,
stopped every subprocess, and found no change in the production recipe tree.
The full local evidence and process logs remain under `/tmp/task568-evidence`;
they are intentionally not committed because logs may contain session material.

## Isolation and fixture disclosure

`task564.example.test` is a reserved synthetic host. The harness substitutes
only its DNS/public-peer decision and maps its HTTP destination to a local
server. The API routes, transaction locks, repair lease, Celery delivery,
scrape task, extraction, staging, checkpoint commits, workflow monitor, and
PostgreSQL reads/writes are production code.

The child deadline is shortened only by updating that queued disposable job's
stored policy before the worker starts. No production constant is changed, no
ambient database or Redis URL is read, no external network call is allowed,
and no production data is written.
