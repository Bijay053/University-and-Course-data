# Selected retry browser/worker canary

## What this proves

`tests/test_selective_retry_canary.py` is opt-in. It composes the existing
`Login`, `AuthProvider`, and `ScrapePromptAssistant` components in a loopback-only
Vite test page. The real auth routes verify a real password hash, deliver a
challenge to a test-only in-memory mail outbox, and consume the OTP through real
private Redis. Password-only access to the assistant must return 401.

The actual assistant scheduler and deterministic retry handler queue the real
`scrape.university` task. A non-eager, single-consumer Celery worker runs against
a private broker. Each case owns a socket-only PostgreSQL schema, applies the
worker-claim and qualification-guidance migrations, and preserves a settled
source job containing one successful pending record and one transient failure.
No shared API, worker, broker, account, or university record is used.

The user instruction selects only the failed URL, with two attempts and real
one-second backoff. The controlled first attempt fails **before** transport.
The second reads Winchester's official LLB Law page with `public_request`:
public-network validation, robots verification, bounded response sizes, no
redirect following, and no challenge/proxy bypass. The transport adapter passes
these verified bytes into the existing source-HTML input of the real extraction
pipeline. It does not return a mocked extraction result.

### Cache cases

* **Valid:** warm the real persisted cache from the real title and tuition
  extractors on the current official HTML. This is deliberately a partial
  deterministic extraction: unknown fields stay unknown. The actual worker
  reuses that exact selected evidence without running the full extractor.
* **Mismatch:** seed those same source-backed facts under older source bytes,
  configuration, audience, and year. The actual worker must miss that entry and
  run the full deterministic extraction on the current official bytes. The old
  cache envelope must remain unchanged.

This is not a claim that every full extraction is cacheable. Unproved or
AI-derived fields still prevent reuse. Only the two explicit deterministic
title methods were added to the cache's method allowlist; all quote, selected
evidence, source URL, normalized-value, and populated-field checks still apply.
Independent cache tests also invalidate each identity dimension separately.

### Assertions

* Independently read a committed `started` attempt reservation before each
  extraction/transport invocation.
* Exactly two attempt rows, error then extracted, with result hashes and at
  least one second between their persisted timestamps.
* Exactly one child-owned pending review row, selected course-owned evidence,
  correct extracted title/tuition, and review-only publication status.
* The complete successful row, evidence, original job/progress, and original
  logs remain byte-for-byte equivalent as JSON.
* No successful-URL extraction or worker progress event.
* Real worker cache-hit events agree with the cache result; zero versus one
  full extraction in the two respective cases.
* Honest completed chat receipt: one pending URL, no unverified URLs, nothing
  published. It remains visible after a real browser reload.
* Block non-private SQL and published-course writes, not merely final row counts.
  Disable optional remote archive writes. Reject all model-client creation.

No model call is needed for this explicit retry command. Therefore this
canary spends zero model USD and does not fabricate a spend reservation.
The existing real-model probe continues to use the shared durable Sydney-day
USD ledger before requests; this canary must not bypass that ledger to add AI.
The official page is fetched once per case, plus its robots check. Extraction
is capped at 45 seconds, chat completion at 150 seconds, and process shutdown
is bounded. Protections or source drift fail the live test rather than passing
using a stored page or pretending recovery succeeded.

## Running

From the workspace root:

```sh
cd backend-py && RUN_SELECTIVE_RETRY_CANARY=1 PYTHONPATH=. python -m pytest \
  tests/test_selective_retry_canary.py -v -s -c /dev/null --noconftest -p no:cacheprovider
```

Requires installed PostgreSQL test binaries, Redis, Playwright Chromium, and the
existing frontend dependencies. Nothing starts beat or the shared worker.
Without the opt-in flag, the live cases are explicitly skipped.

For deterministic retry/cache/lock regression, without live sites or models:

```sh
cd backend-py && PYTHONPATH=. python tests/helpers/run_disposable_redis.py \
  tests/test_selective_retry.py tests/test_selective_retry_capacity.py \
  tests/test_selective_retry_fencing.py tests/test_extraction_result_reuse.py \
  tests/test_full_catalogue_review.py tests/test_worker_fencing.py \
  -q -c /dev/null --noconftest -o asyncio_mode=auto -p no:cacheprovider
```

## Evidence boundaries

Small sanitized acceptance receipts are retained in version control separately:
`docs/selective-retry-workspace-evidence.json` and
`docs/selective-retry-production-evidence.json`. The former records both live
cases and the deterministic regression terminal result; the latter explicitly
records that production acceptance was not run.

Generated artifacts are ignored by Git and deployment:

* `generated-artifacts/selective-retry/valid-workspace.json`
* `generated-artifacts/selective-retry/mismatch-workspace.json`
* Browser screenshots, original public HTML, and Vite logs alongside them.
* `generated-artifacts/selective-retry/production.json` explicitly records
  **not executed**, not a production pass.

Workspace receipts include parser/test content hashes, official-source hashes,
reservation IDs, attempt timestamps/result hashes, child and evidence
ownership, cache hits, and cleanup status. The synthetic content-addressed
release identity enables workspace cache testing; it is **not** a deployment
revision or evidence that production runs this code. Pytest must also finish
successfully so schema/cluster fixture teardown is included in acceptance.

No production connection, course write, approval, publication, or deployment is
part of this test. Workspace evidence must never be presented as production
acceptance.
