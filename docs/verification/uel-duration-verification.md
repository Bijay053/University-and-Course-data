# UEL AI-duration verification

Verified on **2026-09-18**. The duration-contract regression is fixed and the
fresh full isolated run has **zero `InvalidPayloadKeyError` events**, down from
80. **This is not an all-clear for the UEL catalogue.** Two existing template
errors and other source-quality findings remain.

## Production release

Released exact revision
`42d20762680c6260fad7e5a7b52620d9835ae03f` on **2026-09-18** through
`backend-py/deploy/guarded_release.sh`.

- The pre-pull signed smoke passed and the release paused consumers only after
  confirming zero active, reserved, scheduled, queued, running, or
  awaiting-approval scrape work.
- The first attempt stopped before pull when GitHub `main` advanced after
  preflight. The combined tree was integrated and retested; the successful
  attempt deployed the new exact remote tip without bypassing the revision
  fence.
- API and Celery process environments, startup identity checks, Git HEAD, and
  `.release.env` all matched the released full revision. Internal health,
  systemd state, public HTML, and the current frontend asset passed.
- The post-release history API classified a real lifecycle-`completed` job
  with four extraction errors as `extractionQuality.status:
  extraction_errors`, `successful: false`, while preserving lifecycle status.
- The scrape consumer was restored and all worker task counts were zero.
- Two pre-existing tracked operator recipe edits were preserved byte-for-byte
  across the guarded pull. The generated-overlay audit found zero redundant
  overlays.

The post-release read-only UEL review check remained exactly:

- **253 pending reviews / 253 distinct canonical URLs / 0 null canonical URLs**
- Full-row aggregate fingerprint:
  `dfae42188bd00eb4bd085c9de3bd3acd`

No production scrape, approval, or review replacement was performed. The
isolated full-catalogue run was not repeated: its evidence remains tied to the
recorded duration candidate, while the deployed combined revision was covered
by the release, UEL layout, API/history, and frontend regression suites. The
documented source-template, canonical-alias, and field-quality findings still
apply; this release is not a catalogue all-clear.

## Change and tests

The AI fallback's `duration_value` and `duration_unit` are consumed while being
translated to canonical `duration` and `duration_term`. They cannot reach final
payload validation. The shared `payload_contract.py` was **not weakened or
changed**, and the deterministic duration value/unit authority remains intact.

Regressions exercise `extract_course` with actual fallback response shapes,
including years, months, weeks, null aliases, unsupported units, malformed
values, and conflicting deterministic durations. They retain final contract
validation and prohibit real network/browser access. Separate existing tests
cover atomic static duration restoration and unknown-key rejection.

Verification:

- **180 Python tests passed**: 54 duration/fallback, capture, revision-fence,
  API-quality tests plus 126 duration, strict-contract, SIT atomic-duration,
  UEL discovery, variant, and lifecycle tests.
- **40 frontend tests passed** across the job-card and scraping suites,
  including rendered completed-with-errors versus clean-completed cards.
- Frontend TypeScript check passed.
- Preview rendered the expected login gate with no browser errors. Warning
  rendering is verified by the component tests, not by bypassing authentication.

### Post-integration validation

The completion-time rebase onto concurrent application changes removed the
quality-status set definition while leaving its reference in place. The first
configured full regression correctly failed: 20 API/history tests raised
`NameError` (4,411 other tests passed). Those results are not represented as a
successful final-tree validation.

The quality helper now has explicit local terminal-status logic, and a new
mixed-history API regression covers completed/clean, completed/errors,
running, and failed jobs together. On the integrated tree, the six affected
API/history suites pass: **58 passed, one skipped**; the two frontend suites
also pass again (**40 tests**) and TypeScript checking passes.

```bash
cd backend-py
PYTHONPATH=. python -m pytest \
  tests/test_job_quality_reporting.py tests/test_scrape_payload_compat.py \
  tests/test_history_one_requeue.py tests/test_prod_bug_fixes_hijk.py \
  tests/test_scrape_release_history.py tests/test_staged_resume_chain_visibility.py \
  -q -p no:cacheprovider
```

Use the repository pytest configuration for this command: its async-fixture
mode is required by the wider API/history suites. Completion validation reruns
the full configured regression separately.

## Full isolated run

Candidate: `5a2d80f1b27bad862b9836b953590712303a26b8`.
The launcher checked exact committed runtime/config/dependency bytes before
starting. The initial follow-on commit changed only tests and evidence.
Completion subsequently rebased onto concurrent application work, followed by
the API-quality correction described above. The duration extraction pipeline,
UEL variant extractor, and UEL recipe still match the captured candidate.
The live evidence certifies that recorded candidate, not unrelated later
autonomous-repair/orchestrator changes or a new production deployment.

This was **local candidate verification, not a deployment**. Production stayed
on `7e0673caa3fb151694a4f22ba66aeb0356c77544`. The disposable database contained
only a schema clone before the run. Discovery and extraction configuration hashes
matched production; university ID and initial URL intentionally differed.

| Measurement | Previous run | Fresh run |
|---|---:|---:|
| Discovered UG + PG inputs | 276 | 276 |
| Expanded candidates processed | 404 | 404 |
| Pending staged rows | 295 | 369 |
| Exclusions | 27 | 33 |
| All extraction errors | 82 | 2 |
| Duration-key contract errors | 80 | **0** |
| Harness elapsed | 1,070.449 s | **1,188.803 s (19m 49s)** |

Fresh job ran from `03:40:58.598034Z` to `04:00:42.438460Z`.
Accounting is complete: **369 staged + 33 excluded + 2 errors = 404**.
Exclusions: 10 domestic-only, 14 part-time-only, and 9 online-only.

Redis DB 15 was available before launch and throughout the observed run; no
Redis fail-open warnings were logged. This does not certify production fleet
contention or production-worker timing.

## Former duration failures

The baseline's **79 unique lost canonical routes** now reconcile as:

- **73 staged**, all with non-null canonical duration value/unit pairs.
- **6 reach eligibility rejection instead of failing the payload contract**:
  five reported part-time-only and one online-only.
- Course-owned captures independently support five of those exclusions.
  The Autism Spectrum Conditions PGCert has no current course options and
  only generic part-time wording, so its `part_time_only` explanation remains
  **unsubstantiated**, not independently confirmed.

There is also a newly staged SEN Special Schools input whose source canonical
URL points to SEN Inclusion. It is an alias, not an extra independently owned
course. Thus **295 previous rows + 73 recovered rows + 1 alias = 369**.
Do not describe all 369 rows as distinct source courses.

See [the focused source check](uel-duration-recovery-source-check.md).

## Source captures and remaining quality findings

All **276 input URLs** have raw captures, including the previously uncaptured
Autism Spectrum Conditions PGCert fallback path. The full capture set contains
588 detail response observations and two supporting catalogue responses.
Canonical source deduplication yields **275 pages and 405 source route identities**.

The independent report has:

- **No pipeline source URL without a capture**.
- No unexplained source-route drops.
- No repeated exact staged URL IDs; one source-canonical alias described above.
- Two pre-existing whole-page template errors: Physiotherapy attendance and
  Psychology applicant eligibility.
- 61 IELTS differences and 122 eligibility differences across the larger
  recovered review set. These are comparison flags, not proof every value is
  wrong; they include absent/default eligibility and English values.
- 39 differing-capture notices from repeated/alternate transport documents.
  Every raw version is retained. The independent comparator uses the first
  captured document for each canonical source, so these are explicit limits.
- Runtime quality assessment: eight critical and eight warning findings.

The job API preserves lifecycle `status: completed` but reports
`extractionQuality.status: extraction_errors`, `successful: false`,
`errorCount: 2`. Live cards and history use an amber **Completed with errors**
warning, not a green success presentation for such jobs.

## Review safety

Production before/after checks both show:

- **253 pending UEL reviews / 253 distinct canonical URLs**.
- Full-row aggregate fingerprint:
  `dfae42188bd00eb4bd085c9de3bd3acd`.
- Unchanged production release, configuration, and hashed database identity.

No production scrape, deployment, approval, review replacement, or service
restart was performed. The development source database stayed at **35,725**
staged rows. All 369 audit rows remain pending; the audit approval decision is
null. Three autonomous post-run task dispatches were blocked.

## Retained evidence and reproduction

- [Verification summary](uel-duration-verification-result.json)
- [Independent complete catalogue comparison](uel-duration-catalogue-evidence.json)
- [Production before](uel-duration-production-before.json) /
  [after](uel-duration-production-after.json)
- [Configuration parity](uel-duration-config-parity.json)
- [All raw source captures and manifests](uel-duration-source-captures.tar.xz)

The archive includes raw HTML, SHA-256 manifests, all observed transport attempts,
run/provisioning summaries, recovery reconciliation, and the sanitized run log.
It deliberately excludes the database schema dump and environment configuration.
Archive SHA-256:
`dbb1cdaca353d16c911786203250a3cfa9762142a40df1302b16d3382897acbb`.
Unlike an ignored `.local` directory, this tracked archive survives task handoff.

To repeat in the workspace with local Redis running:

```bash
cd backend-py
PYTHONPATH=. python -B scripts/uel_isolated_catalogue_audit.py \
  --expected-local-revision 5a2d80f1b27bad862b9836b953590712303a26b8
PYTHONPATH=. python -B scripts/uel_catalogue_evidence_report.py
```

The candidate mode uses the same exact-revision and clean-runtime safeguards as
release mode but explicitly records `deployment_verified: false`.
For independently verified deployed code, the existing `--expected-release`
mode is unchanged. Neither mode permits approvals or targets the production DB.