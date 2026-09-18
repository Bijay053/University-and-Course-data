# UEL current-template release verification

## Released implementation

Release: `eefc53c64c2acbeb1f8a93403ddad3262e0e4cd9`.

The release includes the narrowly bounded Physiotherapy status-notice and
Psychology linked-course classifications, the route-owned grouped IELTS
parser, and the separately completed no-published-course-options attendance
guard. Regression coverage after integrating those changes passed:

```sh
cd backend-py
PYTHONPATH=. python -m pytest tests/test_uel*.py tests/test_duration.py \
  tests/test_global_delivery_filters.py -q
```

Result: **189 passed**, one pre-existing Pydantic deprecation warning.

## Production recipe preservation

The owner authorized preserving and committing the unrelated production
`bathspa.yaml` and `londonmet.yaml` edits before the final release.
The concurrent duration release temporarily stashed and restored those edits.
The restored bytes were checked before committing; a first attempt using hashes
captured while the files were temporarily stashed correctly aborted without
writing anything.

The restored files were then committed without modifying their contents:

| File | Preserved SHA-256 |
|---|---|
| `backend-py/scraper_config/unis/bathspa.yaml` | `afc201db9d656c3f864ee5cd5deb9c4d9e69f052a4ff1ddc6e88f7e6849ea135` |
| `backend-py/scraper_config/unis/londonmet.yaml` | `bb05cb3ac2bd0d458de25cde16cd363c41444fe99df0342e5abcdce66d5c3869` |

Production Git had no push credential. Its preservation commit was transferred
as a Git bundle to the workspace and pushed through the existing authorized
GitHub remote, retaining the commit and exact bytes. Both recipes and UEL's
recipe load through the configured schema successfully.

## Final guarded release

The checked-in guarded transaction was used with its starting-revision check
bound to the independently observed preservation commit; no safety gates were
removed. It passed:

- Mandatory safe restart smoke against the preceding running release.
- Consumer pause and all-worker/all-job idle checks.
- Clean tracked checkout, target revision, and fast-forward checks.
- Generated-config reconciliation and redundant-overlay audit (zero redundant
  overlays).
- API and worker startup identity checks for the full release SHA.
- `/api/health`, both systemd service checks, and public portal HTML/JS checks.
- Consumer restoration; the worker resumed the `scrape` queue.

The final identity smoke completed at approximately **2026-09-18 05:11:42 UTC**.
Public URL: `https://portal.agentsic.com/`.

Read-only production evidence before and after release found the same **253**
UEL review rows, all pending, with full-row aggregate fingerprint
`dfae42188bd00eb4bd085c9de3bd3acd`. No production UEL scrape, approval, or
review overwrite was performed.

## Post-release isolated audit

The full audit was started only after that final release identity was verified:

```sh
cd backend-py
PYTHONPATH=. python -u -B scripts/uel_isolated_catalogue_audit.py \
  --expected-release eefc53c64c2acbeb1f8a93403ddad3262e0e4cd9
PYTHONPATH=. python -u -B scripts/uel_catalogue_evidence_report.py
```

Run directory: `.local/uel-audit/20260918T051153Z_db2486`.
It uses a fresh schema-only local database and Redis DB 15; autonomous dispatch
is blocked and approvals are prohibited.

### Completed results

The scrape completed at **2026-09-18 05:31:52 UTC**, after 1,179 seconds:

| Check | Result |
|---|---|
| Candidate route inputs processed | 406 / 406 |
| Pending staged records | 373 |
| Explicit exclusions | 33: 11 domestic-only, 9 online-only, 13 part-time-only |
| Extraction errors | 0 |
| Approval decision / non-pending rows | null / 0 |
| Independent canonical source pages / routes | 275 / 405 |
| Parser-versus-independent route enumeration disagreements | 0 |
| Duplicate staged route identities | 0 |
| Captured routes without a staged row or explicit runtime exclusion | 0 |

One preflight transport error and six no-HTML fetch attempts were recorded;
ordinary recovery completed the run with zero final extraction errors.
Autonomous repair/quality/performance tasks were blocked by the isolated runner,
not dispatched to the shared worker.

### Targeted acceptance

All five eligible target records staged pending and match their independently
owned source IELTS statements. The sixth identity, the Home-only Physiotherapy
foundation route, has an exact `rejected: domestic_only` runtime event.

| Route | Attendance / duration | IELTS overall; Writing/Speaking; Listening/Reading |
|---|---|---|
| AI and Data Science MSc | Full Time / 1 year | 6.0; 6.0; 5.5 |
| AI and Data Science MSc with Placement Year | Full Time / 2 years | 6.0; 6.0; 5.5 |
| Physiotherapy Degree | Unknown / unknown | 7.0; 6.5; 6.5 |
| Psychology Degree | Full Time / 3 years | 6.0; 6.0; 5.5 |
| Psychology with foundation year | Full Time / 4 years | 5.5; 5.5; 5.5 |

Physiotherapy Degree retains `international_eligible=true` from its explicit
status notice, while attendance, duration, duration term, fee and intakes are
all null. It does not borrow the Home option's facts. The independent comparison
reports **no differences for any of the five staged target records**.

Fresh source SHA-256 values:

- Physiotherapy: `20f45633701d3adc958e0d7eaa75c0a39869209b521f9b19a16efd13a41ef293`
- Psychology: `eb4df6c936862b38ebfa946f8df8a83711f659ab9779636ec5dc21c4c4f8f4ff`
- AI and Data Science: `09340d675d3ef2fc81253197da431964b3604a76fc840389d7ab5a3c30063166`

After the entire audit, a fresh read-only production check still found all 253
UEL review rows pending with the same full-row fingerprint. The production
tracked checkout was clean and both preserved recipe hashes still matched.
The development source database also retained its original 35,725 review rows;
the new 373 records exist only in the disposable audit database.

### Remaining catalogue-wide findings

This is a pass for the targeted source-template fixes, **not approval of the
whole catalogue**. The independent report still records:

- 60 IELTS comparison differences and 123 eligibility differences across
  other records, including single-award routes using the generic pipeline.
  For example, Applied Theatre MA has source Listening/Reading 5.5 but staged
  6.0; single-award audience evidence often remains null. Differences also
  include defaults where the owned source panel supplies no numeric IELTS.
- One staged PGCE discovery alias not matching the independent canonical source
  identity (`pgce-primary-sen-special-schools` versus
  `pgce-primary-sen-inclusion`). No records were approved or merged to conceal
  this discrepancy.
- 40 repeated-capture byte-difference warnings and 33 template warnings. The
  older independent row walker still flags some now-recognized status/link
  rows; these audit diagnostics are retained rather than suppressed. Raw
  differences must be evaluated separately from substantive course changes.

These findings are outside the bounded sibling-template and parenthetical
IELTS correction. They remain visible in the retained full report.

### Retained evidence

- [Focused acceptance and production preservation](uel-template-release-acceptance.json)
- [Complete catalogue evidence report](uel-template-catalogue-evidence.json)
- [Run summary and runtime counters](uel-template-run-summary.json)
- Raw captures, manifests, provisioning proof, focused acceptance and sanitized
  log: `uel-template-source-captures.tar.xz`, SHA-256
  `a6785a19ca05fdf2affef1bb4cf2f2c5142dc9bb658c07587ab60605a9d945fb`.

The live evidence certifies release
`eefc53c64c2acbeb1f8a93403ddad3262e0e4cd9`; later documentation or task-integration
commits do not retroactively change that provenance.