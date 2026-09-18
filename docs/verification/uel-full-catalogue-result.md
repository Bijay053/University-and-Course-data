# UEL full-catalogue verification — completed, acceptance failed

Verified on **2026-09-18**. The authorized release and fresh full-catalogue
runtime verification were performed. **Do not certify that every UEL course
survives or that every staged IELTS/eligibility value is correct.** The run
found reproducible defects despite the targeted tests passing.

## Release and review safety

- Released `7e0673caa3fb151694a4f22ba66aeb0356c77544` to the verified production
  origin, with fast-forward-only checkout, signed pre-release smoke,
  paused/idle worker fencing, generated-config safeguards, service restart,
  exact API/Celery release-identity checks, API health, and public HTML/asset checks.
- The full scrape ran against a **separate local PostgreSQL database**, cloned
  schema-only. It used the real released `run_scrape`, fresh network requests,
  full discovery, extraction, and actual staging—not mocks or a targeted retry.
- Production and isolated effective **discovery and extraction configuration
  hashes matched**. The intentional identity differences were university ID and
  initial URL (production root versus the undergraduate listing); configured
  undergraduate/postgraduate discovery seeds were identical.
- Production UEL review remained **253 pending rows / 253 unique URLs**.
  Before/after full-row aggregate fingerprint:
  `dfae42188bd00eb4bd085c9de3bd3acd` in both checks.
- The original development database retained **35,724 staged rows** before and
  after. No source data was copied into the disposable database.
- **No real courses were approved.** All 295 audit rows remain pending and the
  audit job approval decision is null. Three post-run background dispatches
  were blocked to prevent autonomous repair/quality tasks.

## Runtime counts and timing

| Measurement | Result |
|---|---:|
| Undergraduate discovery inputs | 114 |
| Postgraduate discovery inputs | 162 |
| Total discovered input URLs | 276 |
| Successful raw UEL preflight captures | 275 |
| Captured canonical source pages after alias deduplication | 274 |
| Expanded runtime candidates processed | 404 / 404 |
| Staged, unique pending route IDs | 295 |
| Deliberate exclusions | 27 |
| Extraction error events | 82 |
| Discovery time reported by job | 7.1 seconds |
| Extraction phase reported by job (includes expansion) | 1,058.2 seconds |
| Staging time reported by job | 1.2 seconds |
| Harness elapsed time | **1,070.449 seconds — 17m 50s** |

Job start: `02:26:10.537690Z`; completion: `02:43:57.602121Z`.
Expansion progress covered all 276 inputs. First/last source-progress timestamps
were `02:26:21.083085Z` / `02:29:47.900166Z`; the resolved-route event was flushed
at `02:30:27.639054Z`. No expansion timeout or catalogue cap occurred. The
released phase budget for 276 sources was 1,800 seconds with four fetch workers.
Cancellation, timeout, and outstanding-task drainage were exercised by automated
tests; the full run itself was intentionally not cancelled.

The job status is `completed`, **not an all-clear quality result**:
`295 staged + 27 excluded + 82 errors = 404 runtime candidates`.

## Independent source-to-route reconciliation

Raw source panels were independently walked with BeautifulSoup; the production
parser was only a second enumeration, not the source of truth for comparison.

| Captured route category | Source identities | Staged | Not staged |
|---|---:|---:|---:|
| UG standard | 114 | 76 | 38 |
| UG foundation | 81 | 74 | 7 |
| PG MA | 31 | 14 | 17 |
| PG MFA | 7 | 7 | 0 |
| PG placement | 40 | 39 | 1 |
| Other PG | 131 | 85 | 46 |
| **Captured total** | **404** | **295** | **109** |

All 295 staged IDs match source identities; none is duplicated or merged with
another staged route. All 109 captured-but-unstaged identities have an explicit
runtime explanation: 27 exclusions, 78 duration-contract failures, and four
routes lost in two whole-page template failures.

The captured-source total and runtime-candidate total happen to both be 404;
they are **not the same set**. Runtime expansion retained a PGCE alias as an
extra input, collapsed each malformed two-route page into one error candidate,
and processed one uncaptured PGCert candidate. Thus the canonical catalogue
contains **at least 405 identities**, not a proven 404-course inventory.

Confirmed alias:
`pgce-primary-sen-special-schools` and `pgce-primary-sen-inclusion` have identical
course/requirements content and canonicalize to the latter. One staged, while
the other hit the duration contract error; there was no duplicate staged row.

## Acceptance failures

### 1. Duration payload contract rejects valid extracted courses

**80 error events / 79 unique canonical route losses** report:

```text
InvalidPayloadKeyError: single_course.extract_course emitted
non-persistable payload key(s): 'duration_unit', 'duration_value'
```

The extra event is the PGCE alias whose canonical sibling staged. This is an
extraction/payload-contract defect, not a timeout or university eligibility
decision. Fix the intermediate AI-field normalization/cleanup without weakening
the shared payload contract; then repeat the full isolated verification.

### 2. Two source templates lose valid sibling routes

- **Physiotherapy:** an explicit international application-status stub has
  blank attendance. Strict whole-page validation rejects both degree and
  foundation identities.
- **Psychology:** a distance-learning course link reuses option-row styling but
  has no applicant/attendance fields. It rejects both otherwise valid routes.

These are reported as extraction errors, **not silently classified domestic**.
Positively identify non-option cards/status stubs without guessing missing
attendance or weakening checks on unexplained partial rows.

### 3. Home-only exclusion is not universal

The 27 runtime exclusions comprise **10 domestic-only, 9 part-time-only,
and 8 online-only**. However, the source audit found **49 staged single-route
rows with null `international_eligible`** despite explicit audience evidence:
48 international-capable and **one Home-only**.

The Home-only exception is **Business Management and Social Justice MRes**
(`/postgraduate/courses/mres-business-management-social-justice`), which staged
as eligibility `ready/ok`. It exists only in the isolated audit's pending data;
it was not approved or written into the production review.

### 4. IELTS source comparison is not a clean pass

After correcting one independent-comparator punctuation false positive,
**26 staged rows** need attention:

- **9 incorrect component values:** owned panels specify overall 6.0,
  writing/speaking 6.0, listening/reading 5.5, but staging stores 6.0 for
  listening/reading. Examples: Applied Theatre MA, Data Science MSc, and
  Education Top Up BA.
- **2 missing component sets:** both AI and Data Science MSc routes say
  `IELTS 6.0 (Writing and Speaking 6.0, Listening and Reading 5.5)`. Overall
  survives; all four components are null. The scoped parser misses
  skill-before-score parenthetical grammar.
- **15 unverified defaults:** staged overall 6.0 is not supported by a numeric
  IELTS statement in the owned source panel. These are mainly PGCE/research
  institutional-default values, not proof of sibling-panel contamination.

All seven MFA rows and 39 of 40 placement routes staged separately; the missing
placement route has explicit Home-only evidence. Passing identity separation
does not establish correctness of every field.

Three source labels lack explicitly owned requirements content: Creative Arts
and Social Justice MRes, Societies and Digital Innovation BSc, and the
Performing Arts distance-learning doctorate. Their omissions must not be filled
from a sibling panel. See the [source findings](uel-source-template-findings.md)
for exact DOM evidence and safe handling distinctions.

## Lifecycle tests and limits

- **40 targeted tests passed in 19.55s** across UEL discovery, variants, and
  lifecycle integration: standard/foundation, placement, MA/MFA, real synthetic
  DB staging/approval, retry, resume, re-extraction, scoped omissions,
  cancellation/deadlines, explicit Home-only, and unknown eligibility.
- **20 additional payload-contract/staged-review tests passed in 48.85s.**
  Existing tests did not cover the live AI duration-key leak or all current
  source grammars. No test rerun can substitute for the failed live evidence.
- After preserving the guarded-release entrypoint and making the audit
  release fence reusable, **107 tests passed in 10.83s**: 58 deployment identity
  tests, 9 audit release-fence tests, and the 40 UEL tests. The fence tests prove
  that later documentation-only commits work while changed/untracked runtime
  code and non-commit/non-exact release identities are rejected.
- Live approval was deliberately not exercised. Approval coverage comes from
  disposable synthetic fixtures; this respects the no-auto-approval instruction.
- This was a local isolated runtime run, **not a production Celery job**.
  Redis was unavailable during the run and its limiters logged fail-open
  warnings. Therefore this report does not certify fleet locks/rate limits or
  production-host timing. The harness now fails preflight when Redis is absent;
  these results are not retroactively represented as having passed that check.
- Browser recovery processed
  `pgcert-autism-spectrum-conditions-learning` outside the successful-preflight
  HTML capture wrapper. Its duration-contract failure is recorded, but its
  complete independent source/IELTS comparison is unverified.

## Evidence and reproduction

Local evidence:
`.local/uel-audit/20260918T022553Z_1910e8/` contains the run/provisioning summaries,
275 raw HTML files and manifest, sanitized run log, and independent evidence
report. Production before/after fingerprints and configuration parity are in
`.local/uel-audit/production-{before,after}.txt` and `config-parity.txt`.
The isolated database is retained for read-only follow-up analysis; it contains
no copied production reviews.

Reusable entrypoints:

The local schema clone used PostgreSQL 17's dump client against PostgreSQL 16.
The launcher removes only the unsupported `SET transaction_timeout = 0` session
setting; strict `ON_ERROR_STOP=1` remains enabled for the restore.

- `backend-py/scripts/uel_isolated_catalogue_audit.py` — guarded local
  schema-only clone and full real run. Requires `--expected-release FULL_SHA`
  from an independently verified deployment. Later documentation/audit-only
  commits are allowed only if the entire application/config/dependency tree
  matches that exact release; the harness revision is recorded separately.
- `backend-py/scripts/uel_isolated_catalogue_runner.py` — real orchestrator,
  no background dispatch/approval, raw-source capture and summaries.
- `backend-py/scripts/uel_catalogue_evidence_report.py` — read-only independent
  source-panel/route/IELTS reconciliation.

To repeat after independently verifying a deployed release and starting local
Redis:

```bash
cd backend-py
PYTHONPATH=. python -B scripts/uel_isolated_catalogue_audit.py \
  --expected-release 7e0673caa3fb151694a4f22ba66aeb0356c77544
PYTHONPATH=. python -B scripts/uel_catalogue_evidence_report.py
```

Use the newly deployed full SHA when validating fixes; do not infer deployment
from local HEAD. The guarded release entrypoint is now tracked at
`backend-py/deploy/guarded_release.sh`; its predecessor fence is intentionally release-specific
and must be updated in an operator's release transaction, not bypassed.
Its contents are byte-identical to the original `.local/prod_pull_all.sh`
template. The location changed because automated completion checkpoints
repeatedly excluded even an explicitly committed `.local/` file. Both deployment
READMEs and the existing safety tests now point to the durable location; no
safety assertions were removed or weakened.

The delivered JSON evidence summary is
[`uel-catalogue-result.json`](uel-catalogue-result.json). This task supplies a
completed verification with failed acceptance findings, **not fixes or an
all-clear certification**.